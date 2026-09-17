"""Bounded compact Stage-A development pilot (K25).

This module is the execution boundary for the compact Stage-A v3 wire.  It is
deliberately *not* a provider client: a caller must inject a model object for
any call to happen.  That makes offline protocol tests safe and prevents a
development run from silently turning into another health probe.

The runner reads only complete pages from the K10 ``linear_stage_packet``
development artifact, selects at most five pages across the available strata,
and gives each page at most one provider attempt.  The persistent
``CallAuthorizationLedger`` is opened before the first attempt and binds the
authorization to the input, scope, settings, model, protocol, and artifact
namespace.  Any subsequent invocation with the same authorization is blocked;
using a new output directory cannot reset the budget.

Provider request bodies and response text exist only in memory.  Persisted
artifacts contain opaque handles, counts, hashes, safe error codes, and
selection metadata.  Stage B/C, production writes, gold data, and frozen data
are outside this runner by construction.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import inspect
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, MutableMapping, Optional, Protocol, Sequence, Tuple, Union

from .compact_stage_a_protocol_v3 import (
    GROUPING_HINTS_FIELD,
    MAX_INPUT_TOKEN_PROXY,
    MAX_OUTPUT_TOKENS,
    MAX_GENERATED_TOPIC_GROUPING_HINTS,
    MAX_TOPIC_GROUPING_HINTS,
    MAX_TOPIC_HINTS_PER_ALIAS,
    MAX_TOPIC_HINTS_PER_RELATION,
    PROTOCOL_VERSION,
    SYSTEM_PROMPT,
    TOPIC_GROUPING_GUIDANCE,
    TOPIC_HINT_KEYS,
    TOPIC_HINT_RELATIONS,
    TOPIC_HINT_RELATION_ORDER,
    TOPIC_HINT_MAX_RELATIONS,
    CompactStageAProtocolV3Error,
    ERROR_TAXONOMY_VERSION,
    KNOWN_VALIDATION_ERROR_CODES,
    build_topic_candidate_hints,
    build_compact_stage_a_request,
    canonical_json,
    measure_output_size,
    measure_wire_size,
    calibrate_token_proxy,
    TOKEN_CALIBRATION_VERSION,
    CALIBRATED_INPUT_TOKEN_PROXY_LIMIT,
    CONTEXT_VALIDATION_TAXONOMY_VERSION,
    CONTEXT_ERROR_TELEMETRY_KEYS,
    build_candidate_connected_components,
    context_validation_telemetry,
    parse_compact_stage_a_output,
    project_body_free_ledger,
    resolve_compact_output,
    stable_hash,
    strict_json_loads,
    _semantic_cue_is_eligible,
    _record_has_substantive_signal,
    _record_has_positive_authority_role,
    _record_is_hard_placeholder,
    _record_is_provider_media,
    _explicit_human_cue,
    _media_caption_source_role_is_valid,
    _message_identity_for_dedup,
    _semantic_primary_aliases_from_packet,
    validate_compact_stage_a_output,
    validation_categories_for_code,
)
from .persistent_call_budget import (
    AuthorizationBindingMismatch,
    CallAuthorizationLedger,
    CallBudgetExceeded,
    ReservationRejected,
)
from .dialogue_segments import has_context_prefix, is_context_only_text


ROOT = Path(__file__).resolve().parents[2]
LOCAL_DAY = "2026-08-25"
# The default input is the current, body-free global candidate signal
# artifact.  Keep the old K10 name below for the compatibility reader used by
# the original synthetic/path tests; it is never used by the current
# stratified path.
LEGACY_INPUT_ARTIFACT_VERSION = "linear_stage_packet_development_v2"
STRATIFIED_SOURCE_ARTIFACT_VERSION = "linear_stage_packet_development_v3_stratified"
CONTEXT_INPUT_ARTIFACT_VERSION = "context_packet_development_v1"
INPUT_ARTIFACT_VERSION = "linear_stage_packet_development_stratified_signals_current"
ARTIFACT_VERSION = "compact_stage_a_development_pilot_v3_stratified_current"
REPORT_SCHEMA_VERSION = "compact_stage_a_development_pilot_report_v3_stratified_current"
RUNNER_SCHEMA_VERSION = "compact_stage_a_development_pilot_runner_v3_stratified_current"
AUTHORIZATION_ID = "STRATIFIED_CURRENT_STAGE_A_V3_PILOT_20260829"
# The original authorization is intentionally immutable.  Adapter/protocol
# repairs use a distinct, explicitly named authorization so a failed old run
# cannot be silently resumed or have its ledger overwritten.
ADAPTER_FIX_AUTHORIZATION_ID = "STRATIFIED_CURRENT_STAGE_A_V3_PILOT_ADAPTER_FIX_20260829"
# Keep the literal available as a named constant as well; callers that record
# authorization policy tend to import the policy name rather than the helper
# alias above.
STRATIFIED_CURRENT_STAGE_A_V3_PILOT_ADAPTER_FIX_20260829 = ADAPTER_FIX_AUTHORIZATION_ID
AUTHORIZATION_ID_ADAPTER_FIX = ADAPTER_FIX_AUTHORIZATION_ID
# The guard-validation budget is intentionally a third, immutable
# authorization.  It is not a continuation of either the original pilot or
# the adapter-fix budget: a caller must opt into this exact id and bind the
# explicit three-page audited subset below.
GUARD_VALIDATION_AUTHORIZATION_ID = "STRATIFIED_CURRENT_STAGE_A_V3_GUARD_VALIDATION_3_20260829"
AUTHORIZATION_ID_GUARD_VALIDATION_3 = GUARD_VALIDATION_AUTHORIZATION_ID
STRATIFIED_CURRENT_STAGE_A_V3_GUARD_VALIDATION_3_20260829 = GUARD_VALIDATION_AUTHORIZATION_ID
# The primary/context repair is a separate preparation contract.  It is kept
# distinct from both the original five-page pilot and the historical
# three-page guard validation so that neither old ledger can be resumed or
# overwritten by a narrower follow-up selection.
PRIMARY_CONTEXT_FIX_AUTHORIZATION_ID = "STRATIFIED_CURRENT_STAGE_A_V3_PRIMARY_CONTEXT_FIX_2_20260830"
AUTHORIZATION_ID_PRIMARY_CONTEXT_FIX_2 = PRIMARY_CONTEXT_FIX_AUTHORIZATION_ID
STRATIFIED_CURRENT_STAGE_A_V3_PRIMARY_CONTEXT_FIX_2_20260830 = PRIMARY_CONTEXT_FIX_AUTHORIZATION_ID
# The guarded five-page pilot is a new, immutable authorization.  It is kept
# separate from the original, adapter-fix, guard-validation, and
# primary/context ledgers so an authorization repair can never resume or
# overwrite an older budget.  The literal alias is intentionally exported for
# callers that treat the policy name itself as the authorization id.
GUARDED_AUTHORIZATION_ID = "STRATIFIED_CURRENT_STAGE_A_V3_PILOT_GUARDED_20260830"
AUTHORIZATION_ID_GUARDED = GUARDED_AUTHORIZATION_ID
STRATIFIED_CURRENT_STAGE_A_V3_PILOT_GUARDED_20260830 = GUARDED_AUTHORIZATION_ID
# The authority-bound projection is a distinct authorization contract.  It is
# deliberately not an alias of the guarded pilot: the projection audit,
# projection implementation, prompt and output namespace are all bound into
# a fresh durable ledger identity.  Keeping this as a literal allowlisted id
# prevents a caller from manufacturing an authorization by supplying an
# arbitrary string.
AUTHORITY_BOUND_AUTHORIZATION_ID = "STRATIFIED_CURRENT_STAGE_A_V3_PILOT_AUTHORITY_BOUND_20260830"
AUTHORIZATION_ID_AUTHORITY_BOUND = AUTHORITY_BOUND_AUTHORIZATION_ID
STRATIFIED_CURRENT_STAGE_A_V3_PILOT_AUTHORITY_BOUND_20260830 = AUTHORITY_BOUND_AUTHORIZATION_ID
# Keep the fresh topic-guided id available while constructing the early
# authorization allow-list; the detailed contract anchors are declared below.
TOPIC_GUIDED_AUTHORIZATION_ID = "STRATIFIED_CURRENT_STAGE_A_V3_PILOT_TOPIC_GUIDED_20260830"
ALLOWED_AUTHORIZATION_IDS = frozenset(
    {
        AUTHORIZATION_ID,
        ADAPTER_FIX_AUTHORIZATION_ID,
        GUARD_VALIDATION_AUTHORIZATION_ID,
        GUARDED_AUTHORIZATION_ID,
        AUTHORITY_BOUND_AUTHORIZATION_ID,
    }
)
ARTIFACT_NAMESPACE = "compact-stage-a-development-v3-stratified-current"
MODEL_ID = "deepseek-v4-flash"
PROVIDER_ID = "openai-compatible"
SOURCE_ID = "injected-compact-stage-a-v3"
RESPONSE_FORMAT_MODE = "omitted"
THINKING_DISABLED = True
MAX_SELECTED_PAGES = 5
MAX_PROVIDER_CALLS = 5
MAX_RETRIES = 0
PER_PAGE_PROVIDER_CALL_LIMIT = 1
MAX_OUTPUT_TOKENS = 400
DEFAULT_INPUT_DIRECTORY = ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY / INPUT_ARTIFACT_VERSION
DEFAULT_ARTIFACT_DIRECTORY = ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY / ARTIFACT_VERSION
GUARD_VALIDATION_ARTIFACT_VERSION = "compact_stage_a_development_pilot_v3_guard_validation_3"
GUARD_VALIDATION_ARTIFACT_NAMESPACE = "compact-stage-a-development-v3-guard-validation-3"
DEFAULT_GUARD_VALIDATION_ARTIFACT_DIRECTORY = ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY / GUARD_VALIDATION_ARTIFACT_VERSION
GUARD_VALIDATION_MAX_SELECTED_PAGES = 3
GUARD_VALIDATION_MAX_PROVIDER_CALLS = 3
GUARD_VALIDATION_PER_PAGE_PROVIDER_CALL_LIMIT = 1
GUARD_VALIDATION_MAX_RETRIES = 0
# This is a preparation-only contract field.  A future independently
# authorized run may use an injected adapter, but this validation contract
# itself performs no SDK/health call and never silently adds one.
GUARD_VALIDATION_SDK_CALLS = 0
GUARD_VALIDATION_PROMPT_VERSION = "compact_stage_a_guard_validation_prompt_v1"
GUARD_VALIDATION_TAXONOMY_VERSION = "compact_stage_a_guard_validation_taxonomy_v1"
# Primary/context fix is intentionally versioned independently.  The artifact
# namespace is the exact user-facing authorization namespace; the persistent
# ledger accepts underscores as part of its safe token grammar.
PRIMARY_CONTEXT_FIX_ARTIFACT_VERSION = "compact_stage_a_development_pilot_v3_primary_context_fix_2"
PRIMARY_CONTEXT_FIX_ARTIFACT_NAMESPACE = "compact_stage_a_development_pilot_v3_primary_context_fix_2"
PRIMARY_CONTEXT_FIX_NAMESPACE = PRIMARY_CONTEXT_FIX_ARTIFACT_NAMESPACE
DEFAULT_PRIMARY_CONTEXT_FIX_ARTIFACT_DIRECTORY = ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY / PRIMARY_CONTEXT_FIX_ARTIFACT_VERSION
PRIMARY_CONTEXT_FIX_MAX_SELECTED_PAGES = 2
PRIMARY_CONTEXT_FIX_MAX_PROVIDER_CALLS = 2
PRIMARY_CONTEXT_FIX_PER_PAGE_PROVIDER_CALL_LIMIT = 1
PRIMARY_CONTEXT_FIX_MAX_RETRIES = 0
PRIMARY_CONTEXT_FIX_SDK_CALLS = 0
PRIMARY_CONTEXT_FIX_PROMPT_VERSION = "compact_stage_a_primary_context_fix_context_unique_prompt_v1"
PRIMARY_CONTEXT_FIX_SCHEMA_VERSION = "compact_stage_a_primary_context_fix_context_unique_schema_v1"
PRIMARY_CONTEXT_FIX_CONTEXT_UNIQUE_PROMPT_VERSION = PRIMARY_CONTEXT_FIX_PROMPT_VERSION
PRIMARY_CONTEXT_FIX_CONTEXT_UNIQUE_SCHEMA_VERSION = PRIMARY_CONTEXT_FIX_SCHEMA_VERSION
PRIMARY_CONTEXT_FIX_TAXONOMY_VERSION = "compact_stage_a_primary_context_fix_taxonomy_v1"
# Guarded mode uses the same compact Stage-A prompt/validator guard code as the
# historical guard validation, but binds that code to the full current
# five-page pilot contract.  Keep the limits explicit even though they equal
# the ordinary pilot limits; this makes the no-supplement policy auditable in
# the persisted contract and prevents a future mode from inheriting a wider
# budget accidentally.
GUARDED_MAX_SELECTED_PAGES = MAX_SELECTED_PAGES
GUARDED_MAX_PROVIDER_CALLS = MAX_PROVIDER_CALLS
GUARDED_PER_PAGE_PROVIDER_CALL_LIMIT = PER_PAGE_PROVIDER_CALL_LIMIT
GUARDED_MAX_RETRIES = MAX_RETRIES
GUARDED_SDK_CALLS = 0
GUARDED_ARTIFACT_VERSION = "compact_stage_a_development_pilot_v3_guarded_20260830"
GUARDED_ARTIFACT_NAMESPACE = "compact-stage-a-development-v3-guarded-20260830"
DEFAULT_GUARDED_ARTIFACT_DIRECTORY = ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY / GUARDED_ARTIFACT_VERSION
GUARDED_PROMPT_VERSION = "compact_stage_a_guarded_prompt_v1"
GUARDED_TAXONOMY_VERSION = "compact_stage_a_guarded_taxonomy_v1"
GUARDED_SYSTEM_PROMPT = (
    SYSTEM_PROMPT
    + " Guarded pilot v1: preserve semantic-primary-only assignment; never "
    "promote a nonsemantic authority/empty row to primary; unresolved "
    "person/object/state candidate cues cap uncertainty at uncertain; emit "
    "only the compact Stage-A topics schema and no guard prose."
)
# Authority-bound mode keeps the compact v3 wire but names the projection
# contract explicitly.  Only the prompt hash is persisted; the prompt body is
# never written to an artifact or ledger.
AUTHORITY_BOUND_MAX_SELECTED_PAGES = MAX_SELECTED_PAGES
AUTHORITY_BOUND_MAX_TOTAL_CALLS = MAX_PROVIDER_CALLS
AUTHORITY_BOUND_MAX_PROVIDER_CALLS = AUTHORITY_BOUND_MAX_TOTAL_CALLS
AUTHORITY_BOUND_PER_PAGE_PROVIDER_CALL_LIMIT = PER_PAGE_PROVIDER_CALL_LIMIT
AUTHORITY_BOUND_MAX_RETRIES = 0
AUTHORITY_BOUND_SUPPLEMENT_CALLS = 0
AUTHORITY_BOUND_SUPPLEMENT_PAGES = 0
AUTHORITY_BOUND_HEALTH_CALLS = 0
AUTHORITY_BOUND_SDK_CALLS = 0
AUTHORITY_BOUND_ARTIFACT_VERSION = "compact_stage_a_development_pilot_v3_authority_bound_20260830"
AUTHORITY_BOUND_ARTIFACT_NAMESPACE = "compact-stage-a-development-v3-authority-bound-20260830"
DEFAULT_AUTHORITY_BOUND_ARTIFACT_DIRECTORY = ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY / AUTHORITY_BOUND_ARTIFACT_VERSION
AUTHORITY_BOUND_PROMPT_VERSION = "compact_stage_a_authority_bound_prompt_v1"
AUTHORITY_BOUND_TAXONOMY_VERSION = "compact_stage_a_authority_bound_taxonomy_v1"
AUTHORITY_BOUND_SYSTEM_PROMPT = (
    SYSTEM_PROMPT
    + " Authority-bound projection v1: use only the reviewed current five-page "
    "selection; preserve source-primary authority bindings and concrete evidence "
    "spans; never promote unbound, candidate-only, empty-authority, media, or "
    "system placeholders. Emit only the compact Stage-A topics schema and no "
    "projection or guard prose."
)
AUTHORITY_BOUND_PROMPT = AUTHORITY_BOUND_SYSTEM_PROMPT
# Kept locally so the pilot can validate a caller-supplied guard category
# without importing a second protocol surface or expanding the provider
# contract.  Values mirror the v3 protocol taxonomy.
VALIDATION_CATEGORIES_FOR_RUNNER = frozenset(
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
GUARD_VALIDATION_SYSTEM_PROMPT = (
    SYSTEM_PROMPT
    + " Guard validation v1: preserve semantic-primary-only assignment; "
    "never promote a nonsemantic authority/empty row to primary; unresolved "
    "person/object/state candidate cues cap uncertainty at uncertain; emit "
    "the same compact Stage-A topics schema and no guard prose."
)
# A short alias is convenient for callers that call the value a prompt rather
# than a system prompt.  The prompt itself is never persisted; only its hash
# and version are part of the authorization binding.
GUARD_VALIDATION_PROMPT = GUARD_VALIDATION_SYSTEM_PROMPT

PRIMARY_CONTEXT_FIX_SYSTEM_PROMPT = (
    SYSTEM_PROMPT
    + " Primary/context fix v1: context_unique=global is mandatory; reject any "
    "context alias that is out of channel or reused across topics; preserve "
    "semantic-primary-only assignment and reject a non-source-primary anchor "
    "or nonsemantic authority/empty row as primary. Emit only the compact "
    "Stage-A topics schema (schema="
    + PRIMARY_CONTEXT_FIX_SCHEMA_VERSION
    + ")."
)
PRIMARY_CONTEXT_FIX_PROMPT = PRIMARY_CONTEXT_FIX_SYSTEM_PROMPT

# The two rows are the only audited targets for this repair preparation.  They
# intentionally retain the source selection ranks (1 and 3) rather than being
# renumbered to 1 and 2.  Rank 4 is a reviewed comparison row, not part of this
# authorization and must never be filled in by a selector fallback.
PRIMARY_CONTEXT_FIX_SUBSET: Tuple[Mapping[str, Any], ...] = (
    {
        "selection_rank": 1,
        "page_ref": "k30_page_07b131d9772084675c3978aa",
        "root_ref": "k30_root_082be555c0b25aa8d48efb30",
        "source_ref": "k30_source_f25b50aa00c1ad3b77f024a8",
        "scope_ref": "k30_scope_b71adbc51f0832b565b7a2a0",
        "cue_family": "candidate_competition",
        "guard_id": "context_unique_failure",
        "expected_error_code": "output_context_item",
        "expected_category": "context_unique",
    },
    {
        "selection_rank": 3,
        "page_ref": "k30_page_000e85fcc0da72ece40da550",
        "root_ref": "k30_root_71e7c8ed572780593967f153",
        "source_ref": "k30_source_f268158d9e37a38793ccd470",
        "scope_ref": "k30_scope_b71adbc51f0832b565b7a2a0",
        "cue_family": "pronoun_person_object_state",
        "guard_id": "non_source_primary_human_fail",
        "expected_error_code": "primary_alias_not_source_primary",
        "expected_category": "selection",
    },
)
PRIMARY_CONTEXT_FIX_PAGE_REFS: Tuple[str, ...] = tuple(
    str(row["page_ref"]) for row in PRIMARY_CONTEXT_FIX_SUBSET
)
PRIMARY_CONTEXT_FIX_SELECTION_RANKS: Tuple[int, ...] = tuple(
    int(row["selection_rank"]) for row in PRIMARY_CONTEXT_FIX_SUBSET
)
PRIMARY_CONTEXT_FIX_ERROR_TAXONOMY: Mapping[str, Mapping[str, str]] = {
    "context_unique_failure": {
        "error_code": "output_context_item",
        "category": "context_unique",
    },
    "non_source_primary_human_fail": {
        "error_code": "primary_alias_not_source_primary",
        "category": "selection",
    },
    # Rank-qualified aliases are accepted for synthetic audit rows.  The
    # canonical subset above uses the shorter stable ids, while these aliases
    # keep the rank-specific audit vocabulary unambiguous without changing the
    # bound page refs or error categories.
    "rank1_context_unique_failure": {
        "error_code": "output_context_item",
        "category": "context_unique",
    },
    "rank3_non_source_primary_human_fail": {
        "error_code": "primary_alias_not_source_primary",
        "category": "selection",
    },
}
PRIMARY_CONTEXT_FIX_GUARD_TAXONOMY = PRIMARY_CONTEXT_FIX_ERROR_TAXONOMY
PRIMARY_CONTEXT_FIX_SUBSET_SHA256 = stable_hash(
    sorted((dict(row) for row in PRIMARY_CONTEXT_FIX_SUBSET), key=lambda row: int(row["selection_rank"]))
)

# The three rows are copied from the adapter-fix human audit + guards replay
# using opaque refs only.  Keep their historical selection ranks so a future
# current-input read can cross-check page/root/source/scope identity before a
# model is even constructed.  The tuple is deliberately immutable at the
# contract boundary; the normalizer below makes fresh dictionaries.
GUARD_VALIDATION_SUBSET: Tuple[Mapping[str, Any], ...] = (
    {
        "selection_rank": 1,
        "page_ref": "k30_page_07b131d9772084675c3978aa",
        "root_ref": "k30_root_082be555c0b25aa8d48efb30",
        "source_ref": "k30_source_f25b50aa00c1ad3b77f024a8",
        "scope_ref": "k30_scope_b71adbc51f0832b565b7a2a0",
        "cue_family": "candidate_competition",
        "guard_id": "pending_unknown_error",
        "expected_error_code": "provider_error",
        "expected_category": "exception",
    },
    {
        "selection_rank": 3,
        "page_ref": "k30_page_000e85fcc0da72ece40da550",
        "root_ref": "k30_root_71e7c8ed572780593967f153",
        "source_ref": "k30_source_f268158d9e37a38793ccd470",
        "scope_ref": "k30_scope_b71adbc51f0832b565b7a2a0",
        "cue_family": "pronoun_person_object_state",
        "guard_id": "no_body_primary",
        "expected_error_code": "primary_alias_not_semantic_eligible",
        "expected_category": "selection",
    },
    {
        "selection_rank": 4,
        "page_ref": "k30_page_01179f02da0288ea58748d7f",
        "root_ref": "k30_root_d768683537177526e93c7536",
        "source_ref": "k30_source_0ff10fc6fc640d1b39d61d3a",
        "scope_ref": "k30_scope_b71adbc51f0832b565b7a2a0",
        "cue_family": "pronoun_person_object_state",
        "guard_id": "unresolved_pronoun_certain",
        "expected_error_code": "uncertainty_overstated_unresolved_reference",
        "expected_category": "enum",
    },
)
GUARD_VALIDATION_PAGE_REFS: Tuple[str, ...] = tuple(
    str(row["page_ref"]) for row in GUARD_VALIDATION_SUBSET
)
GUARD_VALIDATION_ERROR_TAXONOMY: Mapping[str, Mapping[str, str]] = {
    "no_body_primary": {
        "error_code": "primary_alias_not_semantic_eligible",
        "category": "selection",
    },
    "unresolved_pronoun_certain": {
        "error_code": "uncertainty_overstated_unresolved_reference",
        "category": "enum",
    },
    "pending_unknown_error": {
        "error_code": "provider_error",
        "category": "exception",
    },
}
GUARD_VALIDATION_SUBSET_SHA256 = stable_hash(
    sorted((dict(row) for row in GUARD_VALIDATION_SUBSET), key=lambda row: int(row["selection_rank"]))
)
DEFAULT_AUTHORITY_ROOT = ROOT / ".runtime" / "compact-stage-a-authorizations"
DEFAULT_SETTINGS_PATH = ROOT / "data" / "workbench_settings.json"
DEFAULT_CONTEXT_INPUT_DIRECTORY = ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY / "context_packet_development_current"
# Metadata-only evidence from the immutable adapter-fix review.  The runner
# never opens this path for ordinary or synthetic calls; guard mode reads it
# only when a caller explicitly selects the current stratified input path and
# does not supply an in-memory replay override.
DEFAULT_GUARD_VALIDATION_AUDIT_DIRECTORY = (
    ROOT
    / "data"
    / "private"
    / "gold_standard"
    / LOCAL_DAY
    / "compact_stage_a_development_pilot_v3_stratified_current_adapter_fix"
    / "audit"
)
# The primary/context fix consumes the same body-free guard-audit root when a
# caller explicitly supplies the current audited input.  Synthetic tests and
# preparation callers normally inject the three sidecars in memory; keeping a
# named alias makes the provenance binding explicit without reading another
# private range or creating a new artifact.
DEFAULT_PRIMARY_CONTEXT_FIX_AUDIT_DIRECTORY = DEFAULT_GUARD_VALIDATION_AUDIT_DIRECTORY
# Only the audit sidecar belonging to the current linear selection artifact is
# an authorization source.  The repository-root ``artifact/audit`` directory
# contains a historical alias and must never be treated as evidence here.
DEFAULT_AUDIT_DIRECTORY = DEFAULT_INPUT_DIRECTORY / "audit"
# The latest authority-bound projection audit is kept beside the immutable
# guarded review.  It is metadata-only (opaque ids, booleans and SHA-256
# values); the runner opens it only for an explicitly requested authority-bound
# current-input run.  Synthetic callers must pass an in-memory audit mapping
# or receive a synthetic, body-free binding derived from their fixture.
DEFAULT_AUTHORITY_BOUND_AUDIT_DIRECTORY = (
    ROOT
    / "data"
    / "private"
    / "gold_standard"
    / LOCAL_DAY
    / "compact_stage_a_development_pilot_v3_stratified_current_guarded"
    / "audit"
)
AUTHORITY_BOUND_AUDIT_FILENAME = "authority_bound_primary_projection_audit.private.json"
# The topic-guided gate is a metadata-only sidecar beside the authority
# projection audit.  It is read only for an explicit topic-guided current
# input; synthetic callers receive an in-memory body-free witness instead.
DEFAULT_TOPIC_GUIDED_REVIEW_DIRECTORY = DEFAULT_AUTHORITY_BOUND_AUDIT_DIRECTORY
DEFAULT_TOPIC_GUIDED_AUDIT_DIRECTORY = DEFAULT_TOPIC_GUIDED_REVIEW_DIRECTORY
# Opaque page/scope/hash anchors from the latest body-free authority review.
# These are identifiers only; the runner never loads the reviewed packet text
# through this contract surface.
AUTHORITY_BOUND_AUDITED_PAGE_REFS: Tuple[str, ...] = (
    "page_efdaff9b67da0f595ea41948",
    "page_6bed21bcb3fdd77430410081",
    "page_764b7414ecd0b8038c84e36e",
    "page_25a5d4da5b11fe03dcef9610",
    "page_e0d65aa06f15eeeff319629b",
)
AUTHORITY_BOUND_PROJECTION_PAGE_REFS = AUTHORITY_BOUND_AUDITED_PAGE_REFS
AUTHORITY_BOUND_PAGE_REFS = AUTHORITY_BOUND_AUDITED_PAGE_REFS
AUTHORITY_BOUND_AUDIT_SCOPE_REF = "scope_70f24da6a58e1ffd8bcf1598"
AUTHORITY_BOUND_SCOPE_REF = AUTHORITY_BOUND_AUDIT_SCOPE_REF
AUTHORITY_BOUND_SELECTION_SHA256 = "9bfb12fa972bf6b829decd8e43067994977dbd079cec0a01c236cf2d3cbcf8a5"
AUTHORITY_BOUND_CONTEXT_INPUT_SHA256 = "9d52961fee1bbdce51b960457d21cf24665591cf8b9436d7fe068b36b79ef8ab"
AUTHORITY_BOUND_PROJECTION_CODE_SHA256 = "2fc2b9ca61dbd36d1722436f5f722c992d4e44a02b2deae84ac6fde0f812014e"
AUTHORITY_BOUND_AUDIT_SUMMARY_SHA256 = "16c557596c52f5f420653796ef4f2ffcbcad21377a84fb4f14f0a3074429b451"
AUTHORITY_BOUND_HUMAN_AUDIT_SHA256 = "4ac20afd305825ab336a8b40b868b746fba40eb2a90e13da3605521b6f6e8539"
# The authority-bound contract is tied to one body-free source-to-audit
# mapping.  These are opaque handles from the current K30 selection and the
# current projection sidecar; no packet/message text is represented here.
# Keep the mapping ordered and rank-qualified.  Runtime validation below
# compares every selected source row and every sidecar row to this tuple,
# rather than accepting a caller-provided page list as evidence.
AUTHORITY_BOUND_CANONICAL_MAPPING_VERSION = "compact_stage_a_authority_bound_canonical_mapping_v1"
AUTHORITY_BOUND_CANONICAL_MAPPING: Tuple[Mapping[str, str], ...] = (
    {
        "selection_rank": "1",
        "source_page_ref": "k30_page_07b131d9772084675c3978aa",
        "source_root_ref": "k30_root_082be555c0b25aa8d48efb30",
        "source_source_ref": "k30_source_f25b50aa00c1ad3b77f024a8",
        "source_scope_ref": "k30_scope_b71adbc51f0832b565b7a2a0",
        "source_page_hash": "5fa71afb9f8c7e97da689cd000b97914d4ed79ca48c07b6a23402a6bb208fac8",
        "authority_page_ref": "page_efdaff9b67da0f595ea41948",
        "authority_root_ref": "root_f96abcceefc04ac105c5a13d",
        "authority_source_ref": "source_4e22c65f8d248a5af42a8e7f",
        "authority_scope_ref": "scope_70f24da6a58e1ffd8bcf1598",
    },
    {
        "selection_rank": "2",
        "source_page_ref": "k30_page_1dee61714ddfd9680e4166f1",
        "source_root_ref": "k30_root_b3632c1fb6ab2da4deb2f00f",
        "source_source_ref": "k30_source_fcc34ebcb36b73f5c9d166f2",
        "source_scope_ref": "k30_scope_b71adbc51f0832b565b7a2a0",
        "source_page_hash": "fd26954d10fd0ac5f85948ee7add457e3649143fd092c7877d8111fa7e1d340f",
        "authority_page_ref": "page_6bed21bcb3fdd77430410081",
        "authority_root_ref": "root_2dcc50095b37a8a25c8fda10",
        "authority_source_ref": "source_dac47b15bc4f5dba95a78bde",
        "authority_scope_ref": "scope_70f24da6a58e1ffd8bcf1598",
    },
    {
        "selection_rank": "3",
        "source_page_ref": "k30_page_000e85fcc0da72ece40da550",
        "source_root_ref": "k30_root_71e7c8ed572780593967f153",
        "source_source_ref": "k30_source_f268158d9e37a38793ccd470",
        "source_scope_ref": "k30_scope_b71adbc51f0832b565b7a2a0",
        "source_page_hash": "2fef9fafb3ebd27bf5b37db645db64f6be48746d5fa8bb78d86db1f625d9cf3f",
        "authority_page_ref": "page_764b7414ecd0b8038c84e36e",
        "authority_root_ref": "root_2132fcb166a247cf5b68447d",
        "authority_source_ref": "source_e2e7008e33d79fc00680feed",
        "authority_scope_ref": "scope_70f24da6a58e1ffd8bcf1598",
    },
    {
        "selection_rank": "4",
        "source_page_ref": "k30_page_01179f02da0288ea58748d7f",
        "source_root_ref": "k30_root_d768683537177526e93c7536",
        "source_source_ref": "k30_source_0ff10fc6fc640d1b39d61d3a",
        "source_scope_ref": "k30_scope_b71adbc51f0832b565b7a2a0",
        "source_page_hash": "05f502129cb705f13e21bef6334dc61d19f6c4dfee8ccaa5527677a319dfbeba",
        "authority_page_ref": "page_25a5d4da5b11fe03dcef9610",
        "authority_root_ref": "root_d507579999da4a4ac9a8d656",
        "authority_source_ref": "source_8738c083dc51564ed01a617a",
        "authority_scope_ref": "scope_70f24da6a58e1ffd8bcf1598",
    },
    {
        "selection_rank": "5",
        "source_page_ref": "k30_page_0133418e97d2e856f6e592bc",
        "source_root_ref": "k30_root_a28c6d0a0c1c688e6b58f5e2",
        "source_source_ref": "k30_source_add224f04c0ad209e7f00e33",
        "source_scope_ref": "k30_scope_b71adbc51f0832b565b7a2a0",
        "source_page_hash": "40c7c83d59c1415ed814c6c8764c0f400aab4dbd846d8df92e0428158ba92e8a",
        "authority_page_ref": "page_e0d65aa06f15eeeff319629b",
        "authority_root_ref": "root_2bff1bccfbd3be048d773b4f",
        "authority_source_ref": "source_a5fd2acd2ce622befb83f638",
        "authority_scope_ref": "scope_70f24da6a58e1ffd8bcf1598",
    },
)
# Public aliases make the one canonical map unambiguous to audit/test callers;
# all aliases point to the same immutable tuple and are never caller-owned.
AUTHORITY_BOUND_CANONICAL_MAP = AUTHORITY_BOUND_CANONICAL_MAPPING
AUTHORITY_BOUND_PAGE_MAPPING = AUTHORITY_BOUND_CANONICAL_MAPPING
AUTHORITY_BOUND_CANONICAL_PAGE_MAPPING = AUTHORITY_BOUND_CANONICAL_MAPPING
AUTHORITY_BOUND_SCOPE: Mapping[str, str] = {
    "account_id": "ACCOUNT_001",
    "chat_id": "CHAT_006",
}
AUTHORITY_BOUND_SCOPE_SHA256 = "49952c88f9c8c6db3a85c147a5ce63f4b67f31549f1739295543abb8c1e3a765"
AUTHORITY_BOUND_SETTINGS_SHA256 = "f6416630429a98d555cdea21bb39a87209cddf1c27e3dbd4275314ebbcaae826"
AUTHORITY_BOUND_PROMPT_SHA256 = "00e59334ed2c31ec66bd8691137372d0e01f2ed60d4e06eee4b87a2f7020acdc"
# This hash covers the validator/prompt/taxonomy boundary, not private input
# data.  It is checked at runtime so a code or policy drift cannot reuse this
# authorization id.
AUTHORITY_BOUND_CODE_SHA256 = "552f72edc0ec93fc10f2f98d93417d5ec2d60f29e671458701fb57ee2b75c4c0"
# Filled as a literal (rather than recomputed from caller data) so the map
# itself is part of the frozen contract.  The value is verified by a test and
# exported with the other body-free policy anchors.
AUTHORITY_BOUND_CANONICAL_MAPPING_SHA256 = "3c74b07ace939388bd69d8764ce32f5e10704b5eadb9e8be20f77aa8ad6f9a72"

# The topic-guided pilot is a fresh, one-time authorization prepared from the
# latest offline topic-quality gate.  It intentionally reuses the authority
# contract and the same exact five-page source-to-projection map; the new
# namespace and ledger identity make it impossible to resume or overwrite any
# of the historical pilot budgets above.  Keep the policy values explicit so a
# caller cannot manufacture a broader budget by supplying a compatible id.
TOPIC_GUIDED_AUTHORIZATION_ID = "STRATIFIED_CURRENT_STAGE_A_V3_PILOT_TOPIC_GUIDED_20260830"
AUTHORIZATION_ID_TOPIC_GUIDED = TOPIC_GUIDED_AUTHORIZATION_ID
AUTHORIZATION_ID_TOPIC_GUIDED_20260830 = TOPIC_GUIDED_AUTHORIZATION_ID
TOPIC_GUIDED_AUTHORIZATION_ID_20260830 = TOPIC_GUIDED_AUTHORIZATION_ID
STRATIFIED_CURRENT_STAGE_A_V3_PILOT_TOPIC_GUIDED_20260830 = TOPIC_GUIDED_AUTHORIZATION_ID
TOPIC_GUIDED_ARTIFACT_VERSION = "compact_stage_a_development_pilot_v3_topic_guided_20260830"
TOPIC_GUIDED_ARTIFACT_NAMESPACE = "compact_stage_a_development_pilot_v3_topic_guided_20260830"
TOPIC_GUIDED_NAMESPACE = TOPIC_GUIDED_ARTIFACT_NAMESPACE
DEFAULT_TOPIC_GUIDED_ARTIFACT_DIRECTORY = (
    ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY / TOPIC_GUIDED_ARTIFACT_VERSION
)
TOPIC_GUIDED_MAX_SELECTED_PAGES = 5
TOPIC_GUIDED_MAX_TOTAL_CALLS = 5
TOPIC_GUIDED_MAX_PROVIDER_CALLS = TOPIC_GUIDED_MAX_TOTAL_CALLS
TOPIC_GUIDED_PER_PAGE_PROVIDER_CALL_LIMIT = 1
TOPIC_GUIDED_MAX_RETRIES = 0
TOPIC_GUIDED_SUPPLEMENT_CALLS = 0
TOPIC_GUIDED_SUPPLEMENT_PAGES = 0
TOPIC_GUIDED_HEALTH_CALLS = 0
TOPIC_GUIDED_SDK_CALLS = 0
TOPIC_GUIDED_PROMPT_VERSION = "compact_stage_a_topic_guided_prompt_v1"
TOPIC_GUIDED_TAXONOMY_VERSION = "compact_stage_a_topic_guided_taxonomy_v1"
TOPIC_GUIDED_GUIDANCE_VERSION = "compact_stage_a_topic_guided_guidance_v1"
TOPIC_GUIDED_GROUPING_HINTS_VERSION = "compact_stage_a_topic_guided_grouping_hints_v1"
TOPIC_GUIDED_TOKEN_CALIBRATION_VERSION = TOKEN_CALIBRATION_VERSION
TOPIC_GUIDED_CONTEXT_SELECTION_VERSION = "compact_stage_a_topic_guided_context_selection_v1"
TOPIC_GUIDED_CONTEXT_TELEMETRY_VERSION = CONTEXT_VALIDATION_TAXONOMY_VERSION
TOPIC_GUIDED_CONTEXT_SELECTION_PRIORITY = (
    "reply",
    "quote",
    "qa",
    "topic_boundary",
    "opening",
    "adjacent",
    "fallback",
)
TOPIC_GUIDED_REVIEW_FILENAME = "authority_bound_topic_quality_offline_review_final.private.json"
TOPIC_GUIDED_REVIEW_SCHEMA = "compact_stage_a_authority_bound_topic_quality_offline_review_v2"
TOPIC_GUIDED_REVIEW_STATUS = "final_pass"
TOPIC_GUIDED_REVIEW_SHA256 = "91ebd4ad62601f704ae77c98e1a530e4aa48c1b18f1e502f06cf9c1ca7fc4397"
TOPIC_GUIDED_MANIFEST_SHA256 = "224c6087dcae165db0c4e6efe74f66c36fe57813091086dc54fcbb58b5235a6d"
TOPIC_GUIDED_CONTEXT_MANIFEST_SHA256 = "f6fe30a9b489bb83c4ce7da0f0b87e2fa2af9b80c863fde0ac6bc32304aa6c3a"
TOPIC_GUIDED_DEVELOPMENT_SOURCE_SHA256 = "7066ab07306b34a0394c7a531f401e3ce331517d4790ca734d9f152f7ad61331"
TOPIC_GUIDED_PROTOCOL_SOURCE_SHA256 = "5a7bf88586c7213708227c6321ac810c33138223064cfc38b8c4a6d018b90e66"
TOPIC_GUIDED_PREVIOUS_REVIEW_SHA256 = "dbb82f8a1bf4befbe1eeefbd1d75731d2f2be21d0af3c8b84998c3d4cbac906e"
TOPIC_GUIDED_SELECTION_SHA256 = AUTHORITY_BOUND_SELECTION_SHA256
TOPIC_GUIDED_CONTEXT_INPUT_SHA256 = AUTHORITY_BOUND_CONTEXT_INPUT_SHA256
TOPIC_GUIDED_AUTHORITY_PROJECTION_CODE_SHA256 = AUTHORITY_BOUND_PROJECTION_CODE_SHA256
TOPIC_GUIDED_AUTHORITY_AUDIT_SUMMARY_SHA256 = AUTHORITY_BOUND_AUDIT_SUMMARY_SHA256
TOPIC_GUIDED_AUTHORITY_HUMAN_AUDIT_SHA256 = AUTHORITY_BOUND_HUMAN_AUDIT_SHA256
TOPIC_GUIDED_CANONICAL_MAPPING_VERSION = AUTHORITY_BOUND_CANONICAL_MAPPING_VERSION
TOPIC_GUIDED_CANONICAL_MAPPING = AUTHORITY_BOUND_CANONICAL_MAPPING
TOPIC_GUIDED_CANONICAL_MAP = TOPIC_GUIDED_CANONICAL_MAPPING
TOPIC_GUIDED_PAGE_MAPPING = TOPIC_GUIDED_CANONICAL_MAPPING
TOPIC_GUIDED_CANONICAL_PAGE_MAPPING = TOPIC_GUIDED_CANONICAL_MAPPING
TOPIC_GUIDED_CANONICAL_MAPPING_SHA256 = AUTHORITY_BOUND_CANONICAL_MAPPING_SHA256
TOPIC_GUIDED_SCOPE = AUTHORITY_BOUND_SCOPE
TOPIC_GUIDED_SCOPE_SHA256 = AUTHORITY_BOUND_SCOPE_SHA256
TOPIC_GUIDED_SETTINGS_SHA256 = AUTHORITY_BOUND_SETTINGS_SHA256
TOPIC_GUIDED_AUTHORITY_SCOPE_REF = AUTHORITY_BOUND_SCOPE_REF
TOPIC_GUIDED_AUTHORITY_PAGE_REFS = AUTHORITY_BOUND_PAGE_REFS
TOPIC_GUIDED_AUTHORITY_PROJECTION_PAGE_REFS = AUTHORITY_BOUND_PROJECTION_PAGE_REFS

# The prompt and grouping-hint policy are hashed independently so a change to
# either the model guidance or request preflight cannot silently reuse this
# one-time authorization.  The code digest is frozen after the implementation
# below; it is intentionally a literal rather than caller-controlled input.
TOPIC_GUIDED_PROMPT_SHA256 = "2adf3415e84943caeb2e60435c28bec8eac7566396fdd82a1ee2d2d294a7403e"
TOPIC_GUIDED_GUIDANCE_SHA256 = "2fad381d684254e4f25d50791a99168e56fc81efb5fe8ee482082ff742f2a7de"
TOPIC_GUIDED_GROUPING_HINTS_SHA256 = "b8ca82ace952392c7f265d1f431b2f383ddc0de30e879aa237f2d6f25735b636"
TOPIC_GUIDED_CODE_SHA256 = "60f80f496b8b3b3d1dab76486ae8d461cc60229b0b6331ad935e7ff83f629ae6"

TOPIC_GUIDED_SYSTEM_PROMPT = (
    SYSTEM_PROMPT
    + " Topic-guided Stage-A v1: use the supplied g aliases as structural guidance "
    "only; group primary rows when the guidance supports the same topic, retain "
    "multiple primary aliases in one topic when warranted, and keep context "
    "ownership global. Never treat g as a final topic verdict; preserve the "
    "source-primary authority projection and all placeholder/media/system "
    "barriers. Emit only the compact Stage-A topics schema."
)
TOPIC_GUIDED_PROMPT = TOPIC_GUIDED_SYSTEM_PROMPT

# These counts/families are the body-free witness recorded by the latest
# offline gate.  They are enforced for the real current-input path; synthetic
# fixtures are checked against the same shape/range policy without pretending
# to be the reviewed private pages.
TOPIC_GUIDED_EXPECTED_PRIMARY_COUNTS: Tuple[int, ...] = (6, 5, 8, 8, 8)
TOPIC_GUIDED_EXPECTED_G_HINT_COUNTS: Tuple[int, ...] = (4, 4, 4, 1, 4)
TOPIC_GUIDED_EXPECTED_G_RELATIONS: Tuple[str, ...] = ("candidate_context",)
TOPIC_GUIDED_RELATION_FAMILIES: Tuple[str, ...] = tuple(TOPIC_HINT_RELATION_ORDER)
TOPIC_GUIDED_REVIEW_PAGE_REFS: Tuple[str, ...] = (
    "5fa083ba00b65b87",
    "5100f3333c2deef9",
    "8e45e55ec28c3682",
    "0ff46568704dd620",
    "d01226519953c72e",
)

CATEGORY_NAMES: Tuple[str, ...] = (
    "candidate_competition",
    "pronoun_person_object_state",
    "greeting_new_topic",
    "topic_shift",
    "no_reply",
)

# Selection strata are mutually exclusive at the page level.  A page can
# carry several *signals* (for example, candidate history and an opener
# marker), but it is assigned to one canonical stratum only when the
# body-free metadata makes that assignment unambiguous.  In particular,
# candidate-view diversity and an absent reply edge are not, by themselves,
# evidence for two different strata.
_STRATUM_METADATA_KEYS = frozenset(
    {
        "category",
        "categories",
        "category_flag",
        "category_flags",
        "selection_category",
        "selection_categories",
        "selection_stratum",
        "selection_strata",
        "stratum",
        "strata",
    }
)
_STRATUM_DIRECT_KEYS = frozenset(
    {
        "candidate_competition",
        "candidate_competition_high",
        "pronoun_person_object_state",
        "person_object_state",
        "greeting_new_topic",
        "greeting",
        "new_topic",
        "topic_shift",
        "topic_change",
        "shift",
        "no_reply",
        "no-reply",
        "unanswered",
    }
)
_STRATUM_SIGNAL_KEYS = frozenset(
    {
        "candidate_reason",
        "candidate_reasons",
        "reason_code",
        "reason_codes",
        "reasons",
        "relation_subtype",
        "view",
        "view_names",
        "views",
    }
)
_SELECTION_META_KEYS = frozenset(
    {
        # Body-free authoritative role bindings.  These identifiers are
        # retained so the adapter can bind provider aliases to the linear /
        # materialized source-primary set instead of inferring primary from a
        # cue alone.
        "source_primary_message_ids",
        "source_primary_message_handles",
        "source_primary_ids",
        "source_primary_handles",
        "primary_message_ids",
        "primary_message_handles",
        "primary_ids",
        "primary_handles",
        "authority_message_ids",
        "authority_message_handles",
        "authority_ids",
        "authority_handles",
        "adjacent_message_ids",
        "adjacent_message_handles",
        "context_message_ids",
        "context_message_handles",
        "context_ids",
        "context_handles",
        "candidate_views",
        "candidates",
        "candidate_table",
        "messages",
        "message_table",
        "identity_row",
        "fragment_type",
        "is_opener",
        "new_topic",
        "topic_shift",
        "topic_change",
        "segment_id",
        "reply_count",
        "reply_to_message_id",
        "quote_message_id",
    }
)
_SELECTION_SAFE_FIELDS = frozenset(
    {
        "source_primary_message_ids",
        "source_primary_message_handles",
        "source_primary_ids",
        "source_primary_handles",
        "primary_message_ids",
        "primary_message_handles",
        "primary_ids",
        "primary_handles",
        "authority_message_ids",
        "authority_message_handles",
        "authority_ids",
        "authority_handles",
        "adjacent_message_ids",
        "adjacent_message_handles",
        "context_message_ids",
        "context_message_handles",
        "context_ids",
        "context_handles",
        "handle",
        "id",
        "message_handle",
        "message_id",
        "candidate_handle",
        "candidate_id",
        "roles",
        "role",
        "layer",
        "message_role",
        "view_names",
        "views",
        "candidate_reason",
        "candidate_reasons",
        "reason_code",
        "reason_codes",
        "reasons",
        "relation_subtype",
        "category",
        "categories",
        "category_flag",
        "category_flags",
        "selection_category",
        "selection_categories",
        "selection_stratum",
        "selection_strata",
        "stratum",
        "strata",
        "fragment_type",
        "is_opener",
        "new_topic",
        "topic_shift",
        "topic_change",
        "segment_id",
        "reply_count",
        "reply_to_message_id",
        "quote_message_id",
    }
)

OUTPUT_FILENAMES: Dict[str, str] = {
    "manifest": "manifest.private.json",
    "aggregate": "aggregate.private.json",
    "cost": "cost.private.json",
    "ledger": "ledger.private.jsonl",
    "selection": "selection.private.jsonl",
    "decisions": "decisions.private.jsonl",
    "diagnostics": "diagnostics.private.jsonl",
    "errors": "errors.private.jsonl",
}

_FORBIDDEN_PATH_PARTS = frozenset({"frozen", "frozen_test", "frozen-test"})
_BODY_KEYS = frozenset(
    {
        "body",
        "content",
        "content_body",
        "evidence_text",
        "html",
        "markdown",
        "message",
        "messages",
        "prompt",
        "quote",
        "raw",
        "raw_response",
        "raw_text",
        "reasoning",
        "reasoning_content",
        "response",
        "response_text",
        "text",
        "text_body",
        "user_input",
        "user_packet",
    }
)
_SAFE_ERROR_CODES = frozenset(
    {
        "authorization_history_detected",
        "authorization_binding_mismatch",
        "authorization_call_budget_exhausted",
        "candidate_scope_invalid",
        "complete_page_required",
        "cross_chat_scope_violation",
        "input_token_limit_exceeded",
        "model_call_failed",
        "model_unconfigured",
        "output_token_limit_exceeded",
        "page_no_primary_message",
        "page_request_invalid",
        "provider_invalid_json",
        "provider_response_shape",
        "request_primary_messages_empty",
        "schema_validation_failed",
        "settings_hash_missing",
        "stage_b_c_forbidden",
        "input_artifact_invalid",
        "input_artifact_missing",
        "input_manifest_invalid",
        "input_hash_mismatch",
        "audit_invalid",
        "context_input_invalid",
        "context_packet_missing",
        "candidate_only_required",
        "selection_hash_missing",
        "selection_hash_mismatch",
        "provider_primary_role_invalid",
        "model_mismatch",
        "multi_scope_authorization_required",
        "selection_strata_missing",
        "selection_strata_ambiguous",
        "scope_invalid",
        "unsupported_model_interface",
        "unknown_error",
        "guard_audit_invalid",
        "guard_subset_invalid",
        "guard_taxonomy_mismatch",
        "authority_projection_invalid",
        "authority_projection_hash_missing",
        "authority_projection_hash_mismatch",
        "authority_projection_page_drift",
        "authority_projection_scope_drift",
        "authority_projection_code_drift",
        "topic_guided_review_invalid",
        "topic_guided_review_hash_missing",
        "topic_guided_review_hash_mismatch",
        "topic_guided_page_drift",
        "topic_guided_scope_drift",
        "topic_guided_guidance_drift",
        "topic_guided_grouping_hint_drift",
        "topic_guided_protocol_drift",
    }
)
# Keep the runner's execution/authorization codes together with every
# body-free protocol validation code.  The old allow-list only covered a few
# top-level failures, so valid-but-invalid-to-v3 JSON (for example an output
# with an extra key or a duplicated primary alias) was reported as
# ``unknown_error``.
_SAFE_ERROR_CODES = frozenset(
    set(_SAFE_ERROR_CODES)
    | set(KNOWN_VALIDATION_ERROR_CODES)
    | {
        "provider_error",
        "provider_request_failed",
        "provider_sdk_unavailable",
        "provider_unconfigured",
        "provider_response_metadata",
        "stage_call_failed",
        "stage_dependency_pending",
        "context_only_page",
        "body_free_violation",
        "schema_validation_failed",
    }
)
_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")


class CompactStageADevelopmentError(RuntimeError):
    """Safe, body-free runner error."""

    def __init__(self, code: str, *, context_telemetry: Optional[Mapping[str, Any]] = None) -> None:
        self.code = _safe_error_code(code)
        self.validation_categories = validation_categories_for_error_code(self.code)
        self.error_category = self.validation_categories[0]
        self.context_telemetry = _body_free(dict(context_telemetry)) if isinstance(context_telemetry, Mapping) else {}
        self.context_error_counts = dict(self.context_telemetry.get("error_counts", {}))
        self.context_error_flags = dict(self.context_telemetry.get("error_flags", {}))
        super().__init__(self.code)


class CompactStageAModel(Protocol):
    """Minimal injected model interface used by the runner.

    ``complete`` must not persist or mutate the request.  The runner passes
    the fixed system prompt and one compact request mapping; a fake model may
    return a response mapping or JSON text for offline tests.
    """

    model_id: str
    source: str

    def complete(self, system_prompt: str, request: Mapping[str, Any], *, max_output_tokens: int) -> Any:
        ...


@dataclass(frozen=True)
class CompactStageAResponse:
    """In-memory response envelope accepted by synthetic models."""

    content: Any
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    finish_reason: str = "stop"
    model: str = MODEL_ID
    source: str = SOURCE_ID
    reasoning_length: int = 0


@dataclass(frozen=True)
class PageRun:
    page: Mapping[str, Any]
    request: Mapping[str, Any]
    request_sha256: str
    categories: Tuple[str, ...]
    status: str
    payload: Optional[Mapping[str, Any]]
    resolved_payload: Optional[Mapping[str, Any]]
    error_code: Optional[str]
    cache_hit: bool
    provider_call: bool
    input_tokens: int
    output_tokens: int
    latency_ms: float
    model: str
    source: str
    cue_codes: Tuple[str, ...]
    content_length: int = 0
    output_sha256: str = ""
    reasoning_length: int = 0
    finish_reason: str = "not_run"
    validation_categories: Tuple[str, ...] = ()
    error_category: Optional[str] = None
    # Body-free provenance for the role binding used to build this request.
    # The hash covers only opaque source-primary identifiers; no cue/body is
    # retained in either the decision or diagnostic artifact.
    source_primary_eligible: bool = False
    source_primary_sha256: str = ""
    # Body-free source-role versus provider-role projection telemetry.  This
    # is optional for compatibility with existing synthetic PageRun callers.
    role_projection: Mapping[str, Any] = field(default_factory=dict)
    # Body-free deterministic context-view telemetry.  Defaults preserve
    # positional compatibility with existing synthetic PageRun construction.
    context_selection: Mapping[str, Any] = field(default_factory=dict)
    context_error_telemetry: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Existing call sites intentionally use positional construction.  Fill
        # the new taxonomy fields centrally so every pending path (including
        # reservation, selection, and adapter exceptions) gets the same
        # body-free classification without weakening any validator.
        if self.error_code:
            categories = tuple(self.validation_categories) or validation_categories_for_error_code(self.error_code)
            if not self.validation_categories:
                object.__setattr__(self, "validation_categories", categories)
            if not self.error_category:
                object.__setattr__(self, "error_category", categories[0] if categories else "exception")
        if not self.source_primary_sha256:
            raw_hash = self.page.get("_source_primary_sha256")
            if isinstance(raw_hash, str) and raw_hash:
                object.__setattr__(self, "source_primary_sha256", raw_hash)
            else:
                # A PageRun can be constructed directly by a synthetic
                # caller.  Derive a stable empty/set hash without touching a
                # request cue or retaining any body-shaped value.
                binding = _source_primary_binding(self.page, {})
                object.__setattr__(self, "source_primary_sha256", str(binding["sha256"]))
        if not self.source_primary_eligible:
            page_flag = self.page.get("_source_primary_eligible")
            if isinstance(page_flag, bool):
                object.__setattr__(self, "source_primary_eligible", page_flag)
            else:
                primary_rows = [
                    row
                    for row in self.request.get("h", ())
                    if isinstance(row, Mapping) and row.get("k") == "m" and row.get("r") == "p"
                ]
                binding = _source_primary_binding(self.page, {})
                object.__setattr__(
                    self,
                    "source_primary_eligible",
                    bool(primary_rows and binding["provided"]),
                )
        if not self.role_projection:
            page_telemetry = self.page.get("_role_projection_telemetry")
            object.__setattr__(
                self,
                "role_projection",
                dict(page_telemetry)
                if isinstance(page_telemetry, Mapping)
                else _role_projection_telemetry(self.page, self.request),
            )
        if not self.context_selection:
            page_selection = self.page.get("_context_selection_telemetry")
            object.__setattr__(
                self,
                "context_selection",
                dict(page_selection) if isinstance(page_selection, Mapping) else {},
            )
        if not self.context_error_telemetry:
            page_errors = self.page.get("_context_error_telemetry")
            object.__setattr__(
                self,
                "context_error_telemetry",
                dict(page_errors) if isinstance(page_errors, Mapping) else {},
            )

    def to_dict(self) -> Dict[str, Any]:
        role_projection = _body_free(dict(self.role_projection))
        context_selection = _body_free(dict(self.context_selection))
        context_errors = _body_free(dict(self.context_error_telemetry))
        candidate_components = self.page.get("_candidate_component_telemetry")
        candidate_components = _body_free(dict(candidate_components)) if isinstance(candidate_components, Mapping) else {}
        output: Dict[str, Any] = {
            "page_id": str(self.page.get("page_id", "")),
            "root_id": str(self.page.get("root_id", "")),
            "request_sha256": self.request_sha256,
            "categories": list(self.categories),
            "status": self.status,
            "error_code": self.error_code,
            "cache_hit": bool(self.cache_hit),
            "provider_call": bool(self.provider_call),
            "input_tokens": int(self.input_tokens),
            "output_tokens": int(self.output_tokens),
            "latency_ms": round(float(self.latency_ms), 3),
            "model": self.model,
            "source": self.source,
            "cue_codes": list(self.cue_codes),
            "content_length": int(self.content_length),
            "output_sha256": self.output_sha256,
            "reasoning_length": int(self.reasoning_length),
            "finish_reason": self.finish_reason,
            "validation_categories": list(self.validation_categories),
            "error_category": self.error_category,
            "source_primary_eligible": bool(self.source_primary_eligible),
            "source_primary_sha256": self.source_primary_sha256,
            "role_projection": role_projection,
            "context_selection": context_selection,
            "context_error_telemetry": context_errors,
            "context_error_counts": dict(context_errors.get("error_counts", {})),
            "context_error_flags": dict(context_errors.get("error_flags", {})),
            "candidate_components": candidate_components,
            "source_role_counts": dict(role_projection.get("source_role_counts", {})),
            "projected_primary_count": int(role_projection.get("projected_primary_count", 0) or 0),
            "projected_context_count": int(role_projection.get("projected_context_count", 0) or 0),
            "substantive_adjacent_promoted_count": int(role_projection.get("substantive_adjacent_promoted_count", 0) or 0),
            "role_conflict": bool(role_projection.get("role_conflict", False)),
            "topic_limit_cause": str(role_projection.get("topic_limit_cause", "")),
            "telemetry_codes": list(role_projection.get("telemetry_codes", ())),
            "unbound_substantive_candidate_not_promoted_count": int(
                role_projection.get("unbound_substantive_candidate_not_promoted_count", 0) or 0
            ),
            "media_mispromotion_count": int(role_projection.get("media_mispromotion_count", 0) or 0),
            "empty_authority_mispromotion_count": int(role_projection.get("empty_authority_mispromotion_count", 0) or 0),
            "placeholder_mispromotion_count": int(role_projection.get("placeholder_mispromotion_count", 0) or 0),
            "reaction_mispromotion_count": int(role_projection.get("reaction_mispromotion_count", 0) or 0),
            "system_or_event_mispromotion_count": int(role_projection.get("system_or_event_mispromotion_count", 0) or 0),
            "unbound_primary_promotion_count": int(role_projection.get("unbound_primary_promotion_count", 0) or 0),
            "barrier_mispromotion_zero": bool(role_projection.get("barrier_mispromotion_zero", True)),
            "cue_preserved": True,
            "retry_count": 0,
        }
        if self.resolved_payload is not None:
            output["topics"] = deepcopy(list(self.resolved_payload.get("topics", ())))
        return output

    def diagnostic_dict(self) -> Dict[str, Any]:
        """Return per-call scalar telemetry without output/request bodies."""

        role_projection = _body_free(dict(self.role_projection))
        context_selection = _body_free(dict(self.context_selection))
        context_errors = _body_free(dict(self.context_error_telemetry))
        candidate_components = self.page.get("_candidate_component_telemetry")
        candidate_components = _body_free(dict(candidate_components)) if isinstance(candidate_components, Mapping) else {}
        return {
            "page_id": str(self.page.get("page_id", "")),
            "root_id": str(self.page.get("root_id", "")),
            "request_sha256": self.request_sha256,
            "status": self.status,
            "error_code": self.error_code,
            "provider_call": bool(self.provider_call),
            "retry_count": 0,
            "input_tokens": int(self.input_tokens),
            "output_tokens": int(self.output_tokens),
            "latency_ms": round(float(self.latency_ms), 3),
            "content_length": int(self.content_length),
            "output_sha256": self.output_sha256,
            "reasoning_length": int(self.reasoning_length),
            "finish_reason": self.finish_reason,
            "validation_categories": list(self.validation_categories),
            "error_category": self.error_category,
            "source_primary_eligible": bool(self.source_primary_eligible),
            "source_primary_sha256": self.source_primary_sha256,
            "role_projection": role_projection,
            "context_selection": context_selection,
            "context_error_telemetry": context_errors,
            "context_error_counts": dict(context_errors.get("error_counts", {})),
            "context_error_flags": dict(context_errors.get("error_flags", {})),
            "candidate_components": candidate_components,
            "source_role_counts": dict(role_projection.get("source_role_counts", {})),
            "projected_primary_count": int(role_projection.get("projected_primary_count", 0) or 0),
            "projected_context_count": int(role_projection.get("projected_context_count", 0) or 0),
            "substantive_adjacent_promoted_count": int(role_projection.get("substantive_adjacent_promoted_count", 0) or 0),
            "role_conflict": bool(role_projection.get("role_conflict", False)),
            "topic_limit_cause": str(role_projection.get("topic_limit_cause", "")),
            "telemetry_codes": list(role_projection.get("telemetry_codes", ())),
            "unbound_substantive_candidate_not_promoted_count": int(
                role_projection.get("unbound_substantive_candidate_not_promoted_count", 0) or 0
            ),
            "media_mispromotion_count": int(role_projection.get("media_mispromotion_count", 0) or 0),
            "empty_authority_mispromotion_count": int(role_projection.get("empty_authority_mispromotion_count", 0) or 0),
            "placeholder_mispromotion_count": int(role_projection.get("placeholder_mispromotion_count", 0) or 0),
            "reaction_mispromotion_count": int(role_projection.get("reaction_mispromotion_count", 0) or 0),
            "system_or_event_mispromotion_count": int(role_projection.get("system_or_event_mispromotion_count", 0) or 0),
            "unbound_primary_promotion_count": int(role_projection.get("unbound_primary_promotion_count", 0) or 0),
            "barrier_mispromotion_zero": bool(role_projection.get("barrier_mispromotion_zero", True)),
            "model": self.model,
            "source": self.source,
            "cue_preserved": True,
        }


@dataclass(frozen=True)
class CompactStageADevelopmentResult:
    input_directory: str
    output_directory: str
    status: str
    success: bool
    selected_page_count: int
    provider_calls: int
    pending_count: int
    missing_strata: Tuple[str, ...]
    aggregate: Mapping[str, Any]
    artifact_paths: Mapping[str, str]
    selection_diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_directory": self.input_directory,
            "output_directory": self.output_directory,
            "status": self.status,
            "success": bool(self.success),
            "selected_page_count": int(self.selected_page_count),
            "provider_calls": int(self.provider_calls),
            "pending_count": int(self.pending_count),
            "missing_strata": list(self.missing_strata),
            "aggregate": _body_free(self.aggregate),
            "artifact_paths": dict(self.artifact_paths),
            "selection_diagnostics": _body_free(self.selection_diagnostics),
        }


@dataclass(frozen=True)
class CompactStageASelectionPlan:
    """Body-free, deterministic selection plan produced before execution.

    ``selected`` is kept in memory for the runner, while ``to_dict`` exposes
    only opaque page/root ids, canonical strata, counts, and scope labels.
    The plan intentionally distinguishes ``available_strata`` (supported by
    the allowed metadata) from ``missing_strata`` (not covered by the chosen
    single-scope plan); no missing category is inferred from a page count.
    """

    selected: Tuple[Tuple[Mapping[str, Any], Tuple[str, ...]], ...]
    missing_strata: Tuple[str, ...]
    available_strata: Tuple[str, ...]
    page_count: int
    selectable_page_count: int
    selected_scope: Optional[Tuple[str, str]]
    selected_scope_count: int
    scope_coverage: Mapping[str, Mapping[str, Any]]
    stratum_page_counts: Mapping[str, int]
    selected_stratum_counts: Mapping[str, int]
    classification_counts: Mapping[str, int]
    ambiguous_page_ids: Tuple[str, ...]
    unclassified_page_ids: Tuple[str, ...]
    global_coverage_plan_page_ids: Tuple[str, ...]
    global_coverage_plan_scope_count: int
    global_coverage_plan_available: bool
    single_scope_coverage_plan_available: bool
    scope_authorization_required: bool

    def to_dict(self, *, selected_page_limit: Optional[int] = None) -> Dict[str, Any]:
        page_limit = MAX_SELECTED_PAGES if selected_page_limit is None else int(selected_page_limit)
        if page_limit < 0:
            raise ValueError("selected_page_limit_must_be_nonnegative")
        selected_rows = [
            {
                "page_id": str(page.get("page_id", "")),
                "root_id": str(page.get("root_id", "")),
                "scope": {
                    "account_id": str(page.get("scope", {}).get("account_id", ""))
                    if isinstance(page.get("scope"), Mapping)
                    else "",
                    "chat_id": str(page.get("scope", {}).get("chat_id", ""))
                    if isinstance(page.get("scope"), Mapping)
                    else "",
                },
                "categories": list(categories),
                "selection_rank": int(page.get("_selection_rank", index + 1)),
                "source_handle": str(page.get("_source_handle", "")),
                "scope_handle": str(page.get("_scope_handle", "")),
                "candidate_only": bool(page.get("_candidate_only", False)),
                "not_canonical": bool(page.get("_not_canonical", False)),
                "semantic_decision_pending": bool(page.get("_semantic_decision_pending", False)),
            }
            for index, (page, categories) in enumerate(self.selected)
        ]
        return _body_free(
            {
                "selected": selected_rows,
                "selected_page_count": len(selected_rows),
                "selected_page_limit": page_limit,
                "missing_strata": list(self.missing_strata),
                "available_strata": list(self.available_strata),
                "page_count": int(self.page_count),
                "selectable_page_count": int(self.selectable_page_count),
                "classified_page_count": int(self.classification_counts.get("explicit", 0) + self.classification_counts.get("derived", 0)),
                "unclassified_page_count": int(self.classification_counts.get("ambiguous", 0) + self.classification_counts.get("metadata_missing", 0)),
                "selected_scope": {
                    "account_id": self.selected_scope[0],
                    "chat_id": self.selected_scope[1],
                }
                if self.selected_scope is not None
                else None,
                "selected_scope_count": int(self.selected_scope_count),
                "scope_coverage": dict(self.scope_coverage),
                "stratum_page_counts": dict(self.stratum_page_counts),
                "selected_stratum_counts": dict(self.selected_stratum_counts),
                "classification_counts": dict(self.classification_counts),
                "ambiguous_page_ids": list(self.ambiguous_page_ids),
                "unclassified_page_ids": list(self.unclassified_page_ids),
                "global_coverage_plan_page_ids": list(self.global_coverage_plan_page_ids),
                "global_coverage_plan_scope_count": int(self.global_coverage_plan_scope_count),
                "global_coverage_plan_available": bool(self.global_coverage_plan_available),
                "single_scope_coverage_plan_available": bool(self.single_scope_coverage_plan_available),
                "scope_authorization_required": bool(self.scope_authorization_required),
                "selection_rule": "rare_strata_first_then_scope_then_budget",
                "page_categories_exclusive": True,
                "body_free": True,
            }
        )


def _safe_error_code(value: Any) -> str:
    text = str(value or "unknown_error").strip()
    protocol_aliases = {
        "invalid_json": "provider_invalid_json",
        "json_not_text": "provider_invalid_json",
        "duplicate_json_key": "provider_invalid_json",
        "nonstandard_json_number": "provider_invalid_json",
        "output_json_shape": "provider_response_shape",
    }
    text = protocol_aliases.get(text, text)
    if text in _SAFE_ERROR_CODES and _SAFE_CODE_RE.fullmatch(text):
        return text
    # Protocol errors have a useful but finite machine code.  Preserve only
    # the shape, never an arbitrary provider exception message.
    if _SAFE_CODE_RE.fullmatch(text) and len(text) <= 64 and text.endswith(("_invalid", "_failed", "_exceeded")):
        return text
    return "unknown_error"


_RUNNER_ERROR_CATEGORY_OVERRIDES: Mapping[str, Tuple[str, ...]] = {
    "provider_invalid_json": ("json",),
    "provider_request_failed": ("exception",),
    "provider_response_shape": ("schema_keys",),
    "provider_response_metadata": ("exception",),
    "provider_error": ("exception",),
    "provider_sdk_unavailable": ("provider",),
    "provider_unconfigured": ("provider",),
    "model_call_failed": ("exception",),
    "stage_call_failed": ("exception",),
    "unsupported_model_interface": ("exception",),
    "schema_validation_failed": ("schema_keys",),
    "context_only_page": ("input",),
    "stage_dependency_pending": ("input",),
    "body_free_violation": ("body_free",),
    "authorization_history_detected": ("authorization",),
    "authorization_binding_mismatch": ("authorization",),
    "authorization_call_budget_exhausted": ("authorization",),
    "authority_projection_invalid": ("authorization",),
    "authority_projection_hash_missing": ("authorization",),
    "authority_projection_hash_mismatch": ("authorization",),
    "authority_projection_page_drift": ("selection",),
    "authority_projection_scope_drift": ("cross_scope",),
    "authority_projection_code_drift": ("authorization",),
    "topic_guided_review_invalid": ("authorization",),
    "topic_guided_review_hash_missing": ("authorization",),
    "topic_guided_review_hash_mismatch": ("authorization",),
    "topic_guided_page_drift": ("selection",),
    "topic_guided_scope_drift": ("cross_scope",),
    "topic_guided_guidance_drift": ("authorization",),
    "topic_guided_grouping_hint_drift": ("authorization",),
    "topic_guided_protocol_drift": ("authorization",),
    "multi_scope_authorization_required": ("authorization",),
    "selection_strata_missing": ("selection",),
    "selection_strata_ambiguous": ("selection",),
    "guard_audit_invalid": ("selection",),
    "guard_subset_invalid": ("selection",),
    "guard_taxonomy_mismatch": ("other_validation",),
    "input_artifact_invalid": ("input",),
    "input_artifact_missing": ("input",),
    "input_manifest_invalid": ("input",),
    "input_hash_mismatch": ("input",),
    "candidate_only_required": ("input",),
    "candidate_scope_invalid": ("cross_scope",),
    "cross_chat_scope_violation": ("cross_scope",),
    "input_token_limit_exceeded": ("token_limit",),
    "output_token_limit_exceeded": ("token_limit",),
}


def validation_categories_for_error_code(value: Any) -> Tuple[str, ...]:
    """Return a fixed category tuple for persisted runner diagnostics."""

    safe = _safe_error_code(value)
    if safe in _RUNNER_ERROR_CATEGORY_OVERRIDES:
        return _RUNNER_ERROR_CATEGORY_OVERRIDES[safe]
    if safe == "unknown_error":
        # Unknown exception codes are intentionally not surfaced verbatim.
        # They are still diagnosable as an exception, rather than silently
        # masquerading as a protocol validation result.
        return ("exception",)
    return tuple(validation_categories_for_code(safe)) or ("other_validation",)


def error_category_for_code(value: Any) -> str:
    """Return the first stable category for a body-free error code."""

    return validation_categories_for_error_code(value)[0]


def _safe_path(path: Union[str, Path], code: str) -> Path:
    result = Path(path).expanduser().resolve()
    if any(part.casefold() in _FORBIDDEN_PATH_PARTS for part in result.parts):
        raise CompactStageADevelopmentError(code)
    return result


def _file_sha256(path: Union[str, Path]) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def _runner_code_sha256() -> str:
    """Return the code fingerprint used by the repair authorization.

    The hash is computed from this module's bytes and is therefore stable for
    a given checkout while changing whenever the execution boundary changes.
    The deterministic fallback keeps synthetic test environments body-free and
    still supplies a valid SHA-256-shaped binding value if ``__file__`` cannot
    be read.
    """

    return _file_sha256(Path(__file__).resolve()) or stable_hash(
        {"module": __name__, "runner_schema": RUNNER_SCHEMA_VERSION}
    )


def _adapter_binding_metadata(model: Any) -> Dict[str, str]:
    """Describe an injected adapter without retaining request/response data."""

    if model is None:
        adapter_name = "unconfigured"
        adapter_source = SOURCE_ID
        adapter_code_sha256 = stable_hash({"adapter": adapter_name, "source": adapter_source})
    else:
        adapter_type = type(model)
        adapter_name = "%s.%s" % (
            str(getattr(adapter_type, "__module__", "") or "unknown"),
            str(getattr(adapter_type, "__qualname__", "") or adapter_type.__name__),
        )
        adapter_source = str(getattr(model, "source", SOURCE_ID) or SOURCE_ID)
        module = inspect.getmodule(adapter_type)
        module_path = getattr(module, "__file__", None) if module is not None else None
        adapter_code_sha256 = _file_sha256(module_path) if module_path else ""
        if not adapter_code_sha256:
            adapter_code_sha256 = stable_hash(
                {"adapter": adapter_name, "source": adapter_source}
            )
    adapter_identity_sha256 = stable_hash(
        {
            "adapter": adapter_name,
            "source": adapter_source,
            "adapter_code_sha256": adapter_code_sha256,
        }
    )
    return {
        "adapter": adapter_name,
        "adapter_source": adapter_source,
        "adapter_sha256": adapter_identity_sha256,
        "adapter_code_sha256": adapter_code_sha256,
        "code_sha256": _runner_code_sha256(),
    }


def _code_hash_fingerprint(value: Any) -> str:
    """Normalize a scalar or manifest code-hash map to one SHA-256 value."""

    if isinstance(value, Mapping):
        normalized = {
            str(key): str(child).strip().lower()
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
        }
        return stable_hash({"code_hashes": normalized})
    text = str(value or "").strip().lower()
    if _SHA256_RE.fullmatch(text):
        return text
    return stable_hash({"code_hash": text or "not_supplied"})


def _json_read(path: Path, code: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise CompactStageADevelopmentError(code) from exc
    if not isinstance(value, Mapping):
        raise CompactStageADevelopmentError(code)
    return value


def _jsonl_read(path: Path, code: str) -> List[Mapping[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CompactStageADevelopmentError(code) from exc
    rows: List[Mapping[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (TypeError, ValueError) as exc:
            raise CompactStageADevelopmentError(code) from exc
        if not isinstance(value, Mapping):
            raise CompactStageADevelopmentError(code)
        rows.append(value)
    return rows


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    values = [json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for row in rows]
    path.write_text(("\n".join(values) + "\n") if values else "", encoding="utf-8")


def _nonempty(value: Any) -> bool:
    return value not in (None, "", [], (), {})


def _body_free(value: Any) -> Any:
    """Return a copy after rejecting persisted body-shaped keys."""

    def visit(item: Any, path: str = "") -> Any:
        if isinstance(item, Mapping):
            output: Dict[str, Any] = {}
            for raw_key, child in item.items():
                key = str(raw_key)
                folded = key.casefold()
                is_ref = folded.endswith(("_ref", "_refs", "_handle", "_handles"))
                if folded in _BODY_KEYS and _nonempty(child) and not is_ref:
                    raise CompactStageADevelopmentError("body_free_violation")
                output[key] = visit(child, path + key + ".")
            return output
        if isinstance(item, (list, tuple, set, frozenset)):
            return [visit(child, path) for child in item]
        return item

    return visit(value)


_GUARD_SUBSET_FIELDS = frozenset(
    {
        "selection_rank",
        "page_ref",
        "root_ref",
        "source_ref",
        "scope_ref",
        "cue_family",
        "guard_id",
        "expected_error_code",
        "expected_category",
    }
)


def _guard_subset_rows(
    value: Any,
    *,
    require_three: bool = True,
    expected_count: Optional[int] = None,
) -> Tuple[Mapping[str, Any], ...]:
    """Normalize an explicitly audited, opaque guard subset.

    A guard run is never allowed to fall back to the ordinary selector.  The
    caller must therefore provide the exact number of complete opaque
    bindings required by its authorization (three for the historical guard
    contract, two for the primary/context repair).  The normalizer accepts a
    small set of descriptive aliases so callers can pass rows from either the
    source selection map or the adapter-fix human audit without copying any
    body material.
    """

    if isinstance(value, Mapping):
        selected = value.get("selected")
        if isinstance(selected, (list, tuple)):
            value = selected
        elif any(key in value for key in ("page_ref", "page_handle", "page_id")):
            value = [value]
        else:
            raise CompactStageADevelopmentError("guard_subset_invalid")
    if not isinstance(value, (list, tuple)):
        raise CompactStageADevelopmentError("guard_subset_invalid")
    normalized: List[Dict[str, Any]] = []
    seen: set[Tuple[int, str, str, str]] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise CompactStageADevelopmentError("guard_subset_invalid")
        try:
            # Validate the caller's complete row before projecting aliases;
            # otherwise an accidental body-bearing key could be silently
            # discarded and the page would look audited when it was not.
            _body_free(raw)
        except CompactStageADevelopmentError as exc:
            raise CompactStageADevelopmentError("guard_subset_invalid") from exc
        try:
            rank = int(raw.get("selection_rank", raw.get("rank", 0)) or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise CompactStageADevelopmentError("guard_subset_invalid") from exc
        page_ref = str(raw.get("page_ref") or raw.get("page_handle") or raw.get("page_id") or "")
        root_ref = str(raw.get("root_ref") or raw.get("root_handle") or raw.get("root_id") or "")
        source_ref = str(raw.get("source_ref") or raw.get("source_handle") or raw.get("source_packet_id") or "")
        scope_ref = str(raw.get("scope_ref") or raw.get("scope_handle") or "")
        cue_family = str(raw.get("cue_family") or raw.get("family") or raw.get("stratum") or "")
        guard_id = str(raw.get("guard_id") or raw.get("guard") or "")
        expected_code = str(raw.get("expected_error_code") or raw.get("guard_error_code") or "")
        expected_category = str(raw.get("expected_category") or raw.get("guard_category") or "")
        if rank < 1 or not page_ref or not root_ref or not source_ref or not scope_ref or not cue_family or not guard_id:
            raise CompactStageADevelopmentError("guard_subset_invalid")
        if any("\n" in item or "\r" in item or item != item.strip() for item in (page_ref, root_ref, source_ref, scope_ref, cue_family, guard_id, expected_code, expected_category)):
            raise CompactStageADevelopmentError("guard_subset_invalid")
        if expected_code:
            expected_code = _safe_error_code(expected_code)
            if expected_code == "unknown_error":
                raise CompactStageADevelopmentError("guard_subset_invalid")
        if expected_category:
            expected_category = expected_category.strip()
            if expected_category not in VALIDATION_CATEGORIES_FOR_RUNNER:
                raise CompactStageADevelopmentError("guard_subset_invalid")
        key = (rank, page_ref, root_ref, source_ref)
        if key in seen:
            raise CompactStageADevelopmentError("guard_subset_invalid")
        seen.add(key)
        row = {
            "selection_rank": rank,
            "page_ref": page_ref,
            "root_ref": root_ref,
            "source_ref": source_ref,
            "scope_ref": scope_ref,
            "cue_family": cue_family,
            "guard_id": guard_id,
        }
        if expected_code:
            row["expected_error_code"] = expected_code
        if expected_category:
            row["expected_category"] = expected_category
        # Reject body-shaped keys on the supplied audit binding, including
        # unknown nested values, before any page can reach the model path.
        try:
            _body_free(row)
        except CompactStageADevelopmentError as exc:
            raise CompactStageADevelopmentError("guard_subset_invalid") from exc
        normalized.append(row)
    normalized.sort(key=lambda row: int(row["selection_rank"]))
    if expected_count is None and require_three:
        expected_count = GUARD_VALIDATION_MAX_SELECTED_PAGES
    if expected_count is not None and len(normalized) != int(expected_count):
        raise CompactStageADevelopmentError("guard_subset_invalid")
    return tuple(normalized)


def _guard_subset_hash(rows: Sequence[Mapping[str, Any]]) -> str:
    """Hash only canonical opaque subset metadata."""

    return stable_hash([dict(row) for row in rows])


def _guard_ref(row: Mapping[str, Any]) -> Tuple[int, str, str, str, str]:
    try:
        rank = int(row.get("selection_rank", row.get("rank", 0)) or 0)
    except (TypeError, ValueError, OverflowError):
        rank = 0
    return (
        rank,
        str(row.get("page_ref") or row.get("page_handle") or row.get("page_id") or row.get("_page_handle") or ""),
        str(row.get("root_ref") or row.get("root_handle") or row.get("root_id") or row.get("_root_handle") or ""),
        str(row.get("source_ref") or row.get("source_handle") or row.get("source_packet_id") or row.get("_source_handle") or ""),
        str(row.get("scope_ref") or row.get("scope_handle") or row.get("_scope_handle") or ""),
    )


def _guard_replay_rows(replay: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    rows: List[Mapping[str, Any]] = []
    body = replay.get("replay") if isinstance(replay.get("replay"), Mapping) else replay
    for key in ("complete_pages", "pending_pages", "pages"):
        value = body.get(key) if isinstance(body, Mapping) else None
        if isinstance(value, (list, tuple)):
            rows.extend(row for row in value if isinstance(row, Mapping))
    return rows


def _validate_guard_replay(replay: Mapping[str, Any]) -> None:
    """Validate metadata-only adapter-fix guard replay provenance."""

    if not isinstance(replay, Mapping):
        raise CompactStageADevelopmentError("guard_audit_invalid")
    try:
        _body_free(replay)
    except CompactStageADevelopmentError as exc:
        raise CompactStageADevelopmentError("guard_audit_invalid") from exc
    if replay.get("body_free") is False:
        raise CompactStageADevelopmentError("guard_audit_invalid")
    schema = str(replay.get("schema") or "")
    if schema and not schema.startswith("compact_stage_a_adapter_fix_guards_replay"):
        raise CompactStageADevelopmentError("guard_audit_invalid")
    validator = replay.get("validator") if isinstance(replay.get("validator"), Mapping) else {}
    protocol = str(validator.get("protocol_version") or replay.get("protocol") or "")
    if protocol and protocol != PROTOCOL_VERSION:
        raise CompactStageADevelopmentError("guard_audit_invalid")
    conclusion = replay.get("conclusion") if isinstance(replay.get("conclusion"), Mapping) else {}
    if "new_authorization_issued" in conclusion and conclusion.get("new_authorization_issued") is not False:
        raise CompactStageADevelopmentError("guard_audit_invalid")
    if "provider_calls" in conclusion:
        try:
            if int(conclusion.get("provider_calls") or 0) != 0:
                raise CompactStageADevelopmentError("guard_audit_invalid")
        except (TypeError, ValueError) as exc:
            raise CompactStageADevelopmentError("guard_audit_invalid") from exc
    for key in ("rerun_performed", "raw_payload_read", "frozen_read", "gold_loaded", "production_read"):
        if key in conclusion and conclusion.get(key) is not False:
            raise CompactStageADevelopmentError("guard_audit_invalid")
    categories = validator.get("guard_categories") if isinstance(validator.get("guard_categories"), Mapping) else {}
    if categories:
        expected = {
            "primary_alias_not_semantic_eligible": "selection",
            "uncertainty_overstated_unresolved_reference": "enum",
        }
        for code, category in expected.items():
            if code in categories and str(categories.get(code)) != category:
                raise CompactStageADevelopmentError("guard_audit_invalid")
    if not _guard_replay_rows(replay):
        raise CompactStageADevelopmentError("guard_audit_invalid")


def _read_guard_validation_evidence(
    root: Optional[Union[str, Path]],
    *,
    summary_override: Optional[Mapping[str, Any]] = None,
    human_override: Optional[Sequence[Mapping[str, Any]]] = None,
    replay_override: Optional[Mapping[str, Any]] = None,
) -> Tuple[Mapping[str, Any], List[Mapping[str, Any]], Mapping[str, Any], Dict[str, str]]:
    """Load or accept body-free adapter-fix audit/replay evidence only."""

    audit_root: Optional[Path] = None
    if root is not None:
        audit_root = _safe_path(root, "guard_audit_invalid")
    summary_path = audit_root / "audit_summary.private.json" if audit_root is not None else None
    human_path = audit_root / "human_audit.private.jsonl" if audit_root is not None else None
    replay_path = audit_root / "guards_replay.private.json" if audit_root is not None else None
    if summary_override is None:
        if summary_path is None or not summary_path.is_file():
            raise CompactStageADevelopmentError("guard_audit_invalid")
        summary = _json_read(summary_path, "guard_audit_invalid")
        summary_hash = _require_sha256(_file_sha256(summary_path), "guard_audit_invalid")
    else:
        summary = dict(summary_override)
        summary_hash = stable_hash(summary)
    if human_override is None:
        if human_path is None or not human_path.is_file():
            raise CompactStageADevelopmentError("guard_audit_invalid")
        human_rows = _jsonl_read(human_path, "guard_audit_invalid")
        human_hash = _require_sha256(_file_sha256(human_path), "guard_audit_invalid")
    else:
        human_rows = [dict(row) for row in human_override if isinstance(row, Mapping)]
        human_hash = stable_hash(human_rows)
    if replay_override is None:
        if replay_path is None or not replay_path.is_file():
            raise CompactStageADevelopmentError("guard_audit_invalid")
        replay = _json_read(replay_path, "guard_audit_invalid")
        replay_hash = _require_sha256(_file_sha256(replay_path), "guard_audit_invalid")
    else:
        replay = dict(replay_override)
        replay_hash = stable_hash(replay)
    try:
        _body_free(summary)
        _assert_body_free_rows(human_rows, "guard_audit_invalid")
        _validate_guard_replay(replay)
    except CompactStageADevelopmentError as exc:
        raise CompactStageADevelopmentError("guard_audit_invalid") from exc
    privacy = summary.get("privacy") if isinstance(summary.get("privacy"), Mapping) else {}
    if summary.get("body_free") is False or privacy.get("body_free") is False:
        raise CompactStageADevelopmentError("guard_audit_invalid")
    return summary, human_rows, replay, {
        "guard_audit_summary_sha256": summary_hash,
        "guard_human_audit_sha256": human_hash,
        "guards_replay_sha256": replay_hash,
    }


def _validate_guard_audited_subset(
    subset: Sequence[Mapping[str, Any]],
    *,
    available_pages: Sequence[Mapping[str, Any]] = (),
    source_entries: Sequence[Mapping[str, Any]] = (),
    human_rows: Sequence[Mapping[str, Any]] = (),
    guards_replay: Optional[Mapping[str, Any]] = None,
) -> Tuple[Tuple[Mapping[str, Any], ...], str]:
    """Cross-check every subset row against current pages and audit evidence."""

    normalized = _guard_subset_rows(subset)
    expected = {_guard_ref(row) for row in normalized}
    if len(expected) != GUARD_VALIDATION_MAX_SELECTED_PAGES:
        raise CompactStageADevelopmentError("guard_subset_invalid")
    available_refs: set[Tuple[int, str, str, str, str]] = set()
    for raw in available_pages:
        page = raw.get("page") if isinstance(raw.get("page"), Mapping) else raw
        if not isinstance(page, Mapping):
            continue
        rank = int(page.get("_selection_rank", page.get("selection_rank", 0)) or 0)
        available_refs.add(
            (
                rank,
                str(page.get("page_id") or page.get("page_ref") or ""),
                str(page.get("root_id") or page.get("root_ref") or ""),
                str(page.get("_source_handle") or page.get("source_handle") or page.get("source_ref") or page.get("source_packet_id") or ""),
                str(page.get("_scope_handle") or page.get("scope_handle") or page.get("scope_ref") or ""),
            )
        )
    for raw in source_entries:
        if isinstance(raw, Mapping):
            available_refs.add(_guard_ref(raw))
    if available_refs and not expected <= available_refs:
        raise CompactStageADevelopmentError("guard_subset_invalid")
    audit_refs: set[Tuple[int, str, str, str, str]] = set()
    for row in human_rows:
        if not isinstance(row, Mapping):
            continue
        ref = _guard_ref(row)
        if ref[0] and ref[1] and ref[2] and ref[3]:
            # Some compact human ledgers omit scope_ref; match that form by
            # the rank/page/root/source tuple while retaining strict subset
            # scope checks against the current source page above.
            audit_refs.add(ref)
            if not ref[4]:
                audit_refs.add((ref[0], ref[1], ref[2], ref[3], ""))
    for item in normalized:
        ref = _guard_ref(item)
        if ref not in audit_refs and (ref[0], ref[1], ref[2], ref[3], "") not in audit_refs:
            raise CompactStageADevelopmentError("guard_audit_invalid")
    if guards_replay is None:
        raise CompactStageADevelopmentError("guard_audit_invalid")
    _validate_guard_replay(guards_replay)
    replay_rows = _guard_replay_rows(guards_replay)
    replay_refs = {_guard_ref(row) for row in replay_rows}
    for item in normalized:
        ref = _guard_ref(item)
        if ref not in replay_refs and (ref[0], ref[1], ref[2], ref[3], "") not in replay_refs:
            raise CompactStageADevelopmentError("guard_audit_invalid")
        expected_code = str(item.get("expected_error_code") or "")
        expected_category = str(item.get("expected_category") or "")
        guard_id = str(item.get("guard_id") or "")
        taxonomy = GUARD_VALIDATION_ERROR_TAXONOMY.get(guard_id)
        if not isinstance(taxonomy, Mapping):
            raise CompactStageADevelopmentError("guard_subset_invalid")
        if expected_code != str(taxonomy.get("error_code") or "") or expected_category != str(taxonomy.get("category") or ""):
            raise CompactStageADevelopmentError("guard_taxonomy_mismatch")
        categories = validation_categories_for_error_code(expected_code)
        if expected_category not in categories:
            raise CompactStageADevelopmentError("guard_taxonomy_mismatch")
        candidates = [row for row in replay_rows if _guard_ref(row) == ref or _guard_ref(row)[:4] == ref[:4]]
        if not candidates:
            raise CompactStageADevelopmentError("guard_audit_invalid")
        if guard_id == "no_body_primary":
            if not any(
                str((row.get("synthetic_fixture") or {}).get("observed_code") or row.get("observed_code") or "") == "primary_alias_not_semantic_eligible"
                for row in candidates
            ):
                raise CompactStageADevelopmentError("guard_audit_invalid")
        elif guard_id == "unresolved_pronoun_certain":
            if not any(
                str((row.get("synthetic_fixture") or {}).get("observed_code") or row.get("observed_code") or "") == "uncertainty_overstated_unresolved_reference"
                for row in candidates
            ):
                raise CompactStageADevelopmentError("guard_audit_invalid")
        elif guard_id == "pending_unknown_error":
            if not any(
                "pending" in str(row.get("replay_status") or row.get("status") or "").casefold()
                or "pending" in str(row.get("reason") or "").casefold()
                for row in candidates
            ):
                raise CompactStageADevelopmentError("guard_audit_invalid")
        else:
            raise CompactStageADevelopmentError("guard_subset_invalid")
    return normalized, _guard_subset_hash(normalized)


def validate_guard_validation_output(
    value: Any,
    request: Mapping[str, Any],
    *,
    guard_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate one guard-mode response with the stable v3 taxonomy.

    Guard mode still asks for the ordinary compact Stage-A topic shape.  A
    synthetic adapter may additionally return a body-free ``error_code`` (or
    ``guard_error_code``) envelope to exercise a guard without constructing a
    deliberately malformed topic body.  In either form, protocol failures
    retain their known code/category and never expose provider text.
    """

    expected = GUARD_VALIDATION_ERROR_TAXONOMY.get(str(guard_id or ""), {})
    explicit_code: Optional[str] = None
    payload: Any = value
    if isinstance(value, Mapping):
        raw_code = value.get("guard_error_code", value.get("error_code"))
        if raw_code not in (None, "") and "topics" not in value:
            explicit_code = _safe_error_code(raw_code)
            if explicit_code == "unknown_error":
                raise CompactStageADevelopmentError("guard_taxonomy_mismatch")
            expected_code = str(expected.get("error_code") or "")
            if expected_code and explicit_code != expected_code:
                raise CompactStageADevelopmentError("guard_taxonomy_mismatch")
            expected_category = str(expected.get("category") or "")
            if expected_category and expected_category not in validation_categories_for_error_code(explicit_code):
                raise CompactStageADevelopmentError("guard_taxonomy_mismatch")
            raise CompactStageADevelopmentError(explicit_code)
        if "topics" in value and set(value) != {"topics"}:
            # A guard envelope may carry its label, but nothing body-shaped or
            # provider-specific is allowed across the validator boundary.
            payload = {"topics": value.get("topics")}
    try:
        if isinstance(payload, str):
            compact = parse_compact_stage_a_output(payload, request)
        else:
            compact = validate_compact_stage_a_output(payload, request)
    except CompactStageADevelopmentError:
        raise
    except Exception as exc:
        code = _safe_error_code(getattr(exc, "code", None))
        if code == "unknown_error":
            code = "schema_validation_failed"
        raise CompactStageADevelopmentError(code) from exc
    if str(guard_id or "") == "unresolved_pronoun_certain" and request.get("u") != "uncertain":
        # The audit binding says this page is an unresolved pronoun case.  If
        # its request loses the uncertainty ceiling, fail before persisting a
        # seemingly valid answer; the page must be re-audited instead.
        raise CompactStageADevelopmentError("guard_taxonomy_mismatch")
    return compact


# Compatibility aliases for callers that use the shorter terminology.
validate_guard_output = validate_guard_validation_output
validate_guard_response = validate_guard_validation_output


def _guard_validation_code_sha256() -> str:
    """Fingerprint the guard prompt, taxonomy and validator implementation."""

    try:
        source = inspect.getsource(validate_guard_validation_output)
    except (OSError, TypeError):
        source = ""
    return stable_hash(
        {
            "prompt_version": GUARD_VALIDATION_PROMPT_VERSION,
            "prompt_sha256": stable_hash(GUARD_VALIDATION_SYSTEM_PROMPT),
            "taxonomy_version": GUARD_VALIDATION_TAXONOMY_VERSION,
            "taxonomy": GUARD_VALIDATION_ERROR_TAXONOMY,
            "validator_source": source,
            "protocol": PROTOCOL_VERSION,
        }
    )


def _guarded_code_sha256() -> str:
    """Fingerprint the guarded five-page prompt and validator boundary.

    The guarded pilot has its own prompt/version namespace even though it
    reuses the same body-free output validator.  Including the implementation
    source plus the prompt and taxonomy in the authorization binding means a
    code or policy change cannot silently reuse the guarded ledger.
    """

    try:
        source = inspect.getsource(validate_guard_validation_output)
    except (OSError, TypeError):
        source = ""
    return stable_hash(
        {
            "prompt_version": GUARDED_PROMPT_VERSION,
            "prompt_sha256": stable_hash(GUARDED_SYSTEM_PROMPT),
            "taxonomy_version": GUARDED_TAXONOMY_VERSION,
            "taxonomy": GUARD_VALIDATION_ERROR_TAXONOMY,
            "validator_source": source,
            "protocol": PROTOCOL_VERSION,
        }
    )


def _authority_bound_code_sha256() -> str:
    """Fingerprint the authority-bound prompt and validator boundary.

    The authority projection audit is an independent, body-free review.  Its
    code hash is therefore part of the new authorization binding alongside
    this runner's prompt/validator hash.  Include the complete authority
    selection/evidence/frozen-boundary functions and the runner source so a
    source edit cannot silently reopen the authority-bound ledger.
    """

    source_parts: List[str] = []
    for function in (
        validate_guard_validation_output,
        _authority_projection_page_rows,
        _validate_authority_projection_audit,
        _synthetic_authority_projection_audit,
        _authority_bound_canonical_mapping_hash,
        _authority_bound_source_mapping,
        _authority_bound_projection_mapping,
        _authority_bound_validate_canonical_mapping,
        _authority_bound_validate_frozen_boundary,
        _read_authority_projection_evidence,
        _authority_bound_input_binding_hash,
        _authority_bound_contract,
        _build_request,
        _invoke_model,
        _run_one,
        _write_artifacts,
        _validate_provider_primary_role,
        run_compact_stage_a_development_pilot_v3,
    ):
        try:
            source_parts.append(inspect.getsource(function))
        except (OSError, TypeError):
            source_parts.append("")
    return stable_hash(
        {
            "prompt_version": AUTHORITY_BOUND_PROMPT_VERSION,
            "prompt_sha256": stable_hash(AUTHORITY_BOUND_SYSTEM_PROMPT),
            "taxonomy_version": AUTHORITY_BOUND_TAXONOMY_VERSION,
            "taxonomy": GUARD_VALIDATION_ERROR_TAXONOMY,
            "validator_source": "\n".join(source_parts),
            "protocol": PROTOCOL_VERSION,
        }
    )


def _authority_projection_sha256(value: Any, code: str = "authority_projection_hash_missing") -> str:
    """Validate one body-free authority projection digest."""

    text = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(text):
        raise CompactStageADevelopmentError(code)
    return text


def _authority_projection_page_rows(value: Any) -> Tuple[Mapping[str, Any], ...]:
    """Normalize the exact five opaque pages from an authority audit."""

    rows = value.get("pages") if isinstance(value, Mapping) else None
    if not isinstance(rows, list) or len(rows) != AUTHORITY_BOUND_MAX_SELECTED_PAGES:
        raise CompactStageADevelopmentError("authority_projection_page_drift")
    selection_scope = value.get("selection_scope") if isinstance(value, Mapping) else None
    default_scope_ref = (
        str(selection_scope.get("scope_ref") or "")
        if isinstance(selection_scope, Mapping)
        else ""
    )
    normalized: List[Mapping[str, Any]] = []
    ranks: List[int] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise CompactStageADevelopmentError("authority_projection_page_drift")
        try:
            rank = int(row.get("selection_rank") or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise CompactStageADevelopmentError("authority_projection_page_drift") from exc
        page_ref = str(row.get("page_ref") or row.get("page_handle") or "")
        root_ref = str(row.get("root_ref") or row.get("root_handle") or "")
        source_ref = str(row.get("source_ref") or row.get("source_handle") or "")
        if rank < 1 or rank > AUTHORITY_BOUND_MAX_SELECTED_PAGES or not page_ref or not root_ref or not source_ref:
            raise CompactStageADevelopmentError("authority_projection_page_drift")
        if rank in ranks:
            raise CompactStageADevelopmentError("authority_projection_page_drift")
        # Keep only scalar opaque identity/guard fields in the in-memory
        # binding.  The audit itself was checked body-free by the caller.
        normalized.append(
            {
                "selection_rank": rank,
                "page_ref": page_ref,
                "root_ref": root_ref,
                "source_ref": source_ref,
                "scope_ref": str(row.get("scope_ref") or default_scope_ref),
                "page_hash": str(row.get("page_hash") or ""),
                "authority_bound": bool(
                    (row.get("authority_bound_evidence") or {}).get("all_primary_authority_bound")
                    if isinstance(row.get("authority_bound_evidence"), Mapping)
                    else row.get("all_primary_authority_bound", False)
                ),
            }
        )
        ranks.append(rank)
    # Preserve the sidecar's declared order.  Sorting first would make a
    # swapped or otherwise reordered projection look valid and would defeat
    # the rank-to-page canonical mapping below.
    if ranks != list(range(1, AUTHORITY_BOUND_MAX_SELECTED_PAGES + 1)):
        raise CompactStageADevelopmentError("authority_projection_page_drift")
    normalized.sort(key=lambda row: int(row["selection_rank"]))
    if [int(row["selection_rank"]) for row in normalized] != list(range(1, AUTHORITY_BOUND_MAX_SELECTED_PAGES + 1)):
        raise CompactStageADevelopmentError("authority_projection_page_drift")
    if any(not str(row.get("scope_ref") or "") for row in normalized):
        raise CompactStageADevelopmentError("authority_projection_scope_drift")
    if len({str(row.get("scope_ref") or "") for row in normalized}) != 1:
        raise CompactStageADevelopmentError("authority_projection_scope_drift")
    if any(not bool(row.get("authority_bound")) for row in normalized):
        raise CompactStageADevelopmentError("authority_projection_invalid")
    return tuple(normalized)


def _authority_bound_canonical_mapping_hash() -> str:
    """Hash the frozen body-free source-to-authority page mapping."""

    return stable_hash([dict(row) for row in AUTHORITY_BOUND_CANONICAL_MAPPING])


def _authority_bound_source_mapping(
    selected: Sequence[Tuple[Mapping[str, Any], Tuple[str, ...]]],
) -> Tuple[Mapping[str, Any], ...]:
    """Cross-check selected source rows against the frozen K30 mapping.

    The source selector is not evidence by itself.  Every rank, page/root/
    source/scope handle and body-free page digest must match the one audited
    K30 row.  This runs before reading a ledger or constructing a model.
    """

    if _authority_bound_canonical_mapping_hash() != AUTHORITY_BOUND_CANONICAL_MAPPING_SHA256:
        raise CompactStageADevelopmentError("authority_projection_code_drift")
    if len(selected) != AUTHORITY_BOUND_MAX_SELECTED_PAGES:
        raise CompactStageADevelopmentError("authority_projection_page_drift")
    actual_rows: List[Mapping[str, Any]] = []
    ranks: List[int] = []
    identity_rows: List[Tuple[int, str, str, str, str]] = []
    for index, (page, _categories) in enumerate(selected, 1):
        if not isinstance(page, Mapping):
            raise CompactStageADevelopmentError("authority_projection_page_drift")
        try:
            rank = int(page.get("_selection_rank", page.get("selection_rank", index)) or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise CompactStageADevelopmentError("authority_projection_page_drift") from exc
        page_ref = str(page.get("page_id") or page.get("page_ref") or "")
        root_ref = str(page.get("root_id") or page.get("root_ref") or "")
        source_ref = str(
            page.get("_source_handle")
            or page.get("source_handle")
            or page.get("source_ref")
            or ""
        )
        scope_ref = str(
            page.get("_scope_handle")
            or page.get("scope_handle")
            or page.get("scope_ref")
            or ""
        )
        page_hash = str(page.get("page_hash") or "").strip().lower()
        if not page_ref or not root_ref or not source_ref or not scope_ref or not page_hash:
            raise CompactStageADevelopmentError("authority_projection_page_drift")
        try:
            actual_page_scope = _scope(page.get("scope"))
        except CompactStageADevelopmentError as exc:
            raise CompactStageADevelopmentError("authority_projection_scope_drift") from exc
        if actual_page_scope != dict(AUTHORITY_BOUND_SCOPE):
            raise CompactStageADevelopmentError("authority_projection_scope_drift")
        ranks.append(rank)
        identity_rows.append((rank, page_ref, root_ref, source_ref, scope_ref))
        actual_rows.append(
            {
                "selection_rank": rank,
                "source_page_ref": page_ref,
                "source_root_ref": root_ref,
                "source_source_ref": source_ref,
                "source_scope_ref": scope_ref,
                "source_page_hash": page_hash,
            }
        )
    if ranks != list(range(1, AUTHORITY_BOUND_MAX_SELECTED_PAGES + 1)):
        raise CompactStageADevelopmentError("authority_projection_page_drift")
    if len(set(identity_rows)) != AUTHORITY_BOUND_MAX_SELECTED_PAGES:
        raise CompactStageADevelopmentError("authority_projection_page_drift")
    for actual, expected in zip(actual_rows, AUTHORITY_BOUND_CANONICAL_MAPPING):
        if actual["source_scope_ref"] != str(expected["source_scope_ref"]):
            raise CompactStageADevelopmentError("authority_projection_scope_drift")
        if actual["selection_rank"] != int(expected["selection_rank"]) or any(
            actual[key] != str(expected[key])
            for key in ("source_page_ref", "source_root_ref", "source_source_ref", "source_page_hash")
        ):
            raise CompactStageADevelopmentError("authority_projection_page_drift")
    return tuple(actual_rows)


def _authority_bound_projection_mapping(
    rows: Sequence[Mapping[str, Any]],
) -> Tuple[Mapping[str, Any], ...]:
    """Cross-check authority-sidecar rows against the frozen map."""

    if len(rows) != AUTHORITY_BOUND_MAX_SELECTED_PAGES:
        raise CompactStageADevelopmentError("authority_projection_page_drift")
    actual_rows: List[Mapping[str, Any]] = []
    ranks: List[int] = []
    identities: List[Tuple[int, str, str, str, str]] = []
    for row in rows:
        try:
            rank = int(row.get("selection_rank") or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise CompactStageADevelopmentError("authority_projection_page_drift") from exc
        page_ref = str(row.get("page_ref") or "")
        root_ref = str(row.get("root_ref") or "")
        source_ref = str(row.get("source_ref") or "")
        scope_ref = str(row.get("scope_ref") or "")
        if not page_ref or not root_ref or not source_ref or not scope_ref:
            raise CompactStageADevelopmentError("authority_projection_page_drift")
        ranks.append(rank)
        identities.append((rank, page_ref, root_ref, source_ref, scope_ref))
        actual_rows.append(
            {
                "selection_rank": rank,
                "authority_page_ref": page_ref,
                "authority_root_ref": root_ref,
                "authority_source_ref": source_ref,
                "authority_scope_ref": scope_ref,
            }
        )
    if ranks != list(range(1, AUTHORITY_BOUND_MAX_SELECTED_PAGES + 1)):
        raise CompactStageADevelopmentError("authority_projection_page_drift")
    if len(set(identities)) != AUTHORITY_BOUND_MAX_SELECTED_PAGES:
        raise CompactStageADevelopmentError("authority_projection_page_drift")
    for actual, expected in zip(actual_rows, AUTHORITY_BOUND_CANONICAL_MAPPING):
        if actual["authority_scope_ref"] != str(expected["authority_scope_ref"]):
            raise CompactStageADevelopmentError("authority_projection_scope_drift")
        if actual["selection_rank"] != int(expected["selection_rank"]) or any(
            actual[key] != str(expected[key])
            for key in ("authority_page_ref", "authority_root_ref", "authority_source_ref")
        ):
            raise CompactStageADevelopmentError("authority_projection_page_drift")
    return tuple(actual_rows)


def _authority_bound_validate_canonical_mapping(
    selected: Sequence[Tuple[Mapping[str, Any], Tuple[str, ...]]],
    projection_rows: Sequence[Mapping[str, Any]],
) -> Tuple[Mapping[str, Any], ...]:
    """Bind source K30 identity to the matching authority-sidecar identity."""

    expected_authority_pages = tuple(
        str(row["authority_page_ref"]) for row in AUTHORITY_BOUND_CANONICAL_MAPPING
    )
    if (
        expected_authority_pages != tuple(AUTHORITY_BOUND_AUDITED_PAGE_REFS)
        or expected_authority_pages != tuple(AUTHORITY_BOUND_PROJECTION_PAGE_REFS)
        or expected_authority_pages != tuple(AUTHORITY_BOUND_PAGE_REFS)
    ):
        raise CompactStageADevelopmentError("authority_projection_code_drift")
    source_rows = _authority_bound_source_mapping(selected)
    authority_rows = _authority_bound_projection_mapping(projection_rows)
    combined: List[Mapping[str, Any]] = []
    for source, authority, expected in zip(source_rows, authority_rows, AUTHORITY_BOUND_CANONICAL_MAPPING):
        if int(source["selection_rank"]) != int(authority["selection_rank"]):
            raise CompactStageADevelopmentError("authority_projection_page_drift")
        combined.append(
            {
                # The frozen map uses string ranks as a JSON-safe scalar; the
                # runtime checks above already validated the integer rank.
                "selection_rank": str(expected["selection_rank"]),
                "source_page_ref": str(source["source_page_ref"]),
                "source_root_ref": str(source["source_root_ref"]),
                "source_source_ref": str(source["source_source_ref"]),
                "source_scope_ref": str(source["source_scope_ref"]),
                "source_page_hash": str(source["source_page_hash"]),
                "authority_page_ref": str(authority["authority_page_ref"]),
                "authority_root_ref": str(authority["authority_root_ref"]),
                "authority_source_ref": str(authority["authority_source_ref"]),
                "authority_scope_ref": str(authority["authority_scope_ref"]),
            }
        )
    if stable_hash(combined) != AUTHORITY_BOUND_CANONICAL_MAPPING_SHA256:
        # The canonical tuple hash intentionally covers the same ten opaque
        # fields as ``combined``.  A mismatch means the map or its ordering was
        # edited even if the individual rows happened to look plausible.
        raise CompactStageADevelopmentError("authority_projection_code_drift")
    return tuple(combined)


def _validate_authority_projection_audit(value: Mapping[str, Any]) -> Tuple[Tuple[Mapping[str, Any], ...], Dict[str, str]]:
    """Validate the latest body-free authority-bound projection audit.

    This function intentionally inspects metadata only.  It never opens a
    packet, message, provider response, frozen split, or gold annotation.
    """

    try:
        _body_free(value)
    except CompactStageADevelopmentError as exc:
        raise CompactStageADevelopmentError("authority_projection_invalid") from exc
    if value.get("body_free") is False:
        raise CompactStageADevelopmentError("authority_projection_invalid")
    schema = str(value.get("schema") or "")
    if schema and schema != "compact_stage_a_authority_bound_primary_projection_audit_v2":
        raise CompactStageADevelopmentError("authority_projection_invalid")
    if str(value.get("audit_status") or "") not in {"pass_exact_underprojection_replay", "pass"}:
        raise CompactStageADevelopmentError("authority_projection_invalid")
    if value.get("gate") is False:
        raise CompactStageADevelopmentError("authority_projection_invalid")
    acceptance = value.get("acceptance") if isinstance(value.get("acceptance"), Mapping) else {}
    if acceptance and str(acceptance.get("status") or "") != "pass":
        raise CompactStageADevelopmentError("authority_projection_invalid")
    for key in ("authority_bound_evidence_gate", "difference_binding_gate", "all_scopes_bound"):
        if acceptance and acceptance.get(key) is not True:
            raise CompactStageADevelopmentError("authority_projection_invalid")
    boundaries = value.get("execution_boundaries") if isinstance(value.get("execution_boundaries"), Mapping) else {}
    for key in ("frozen_read", "frozen_test_read", "gold_loaded", "production_state_written", "provider_called", "source_artifact_written"):
        if boundaries.get(key) is True:
            raise CompactStageADevelopmentError("authority_projection_invalid")
    try:
        if int(boundaries.get("provider_call_count") or 0) != 0:
            raise CompactStageADevelopmentError("authority_projection_invalid")
    except (TypeError, ValueError, OverflowError) as exc:
        raise CompactStageADevelopmentError("authority_projection_invalid") from exc

    scope = value.get("selection_scope") if isinstance(value.get("selection_scope"), Mapping) else {}
    if scope:
        if scope.get("scope_ref") not in (None, "") and str(scope.get("scope_ref")) != AUTHORITY_BOUND_SCOPE_REF:
            raise CompactStageADevelopmentError("authority_projection_scope_drift")
        if scope.get("selected_page_count") not in (None, AUTHORITY_BOUND_MAX_SELECTED_PAGES):
            raise CompactStageADevelopmentError("authority_projection_page_drift")
        if scope.get("same_five_page_materialize") is not True or scope.get("selection_binding_verified") is not True:
            raise CompactStageADevelopmentError("authority_projection_page_drift")
        for key in ("candidate_only", "not_canonical", "semantic_decision_pending", "development_only", "single_scope"):
            if scope.get(key) is False:
                raise CompactStageADevelopmentError("authority_projection_invalid")
    rows = _authority_projection_page_rows(value)
    if scope:
        scope_refs = scope.get("page_refs")
        if isinstance(scope_refs, list) and [str(item) for item in scope_refs] != [str(row["page_ref"]) for row in rows]:
            raise CompactStageADevelopmentError("authority_projection_page_drift")

    lineage = value.get("input_lineage") if isinstance(value.get("input_lineage"), Mapping) else {}
    digest_names = {
        "authority_projection_selection_sha256": lineage.get("selection_sha256") or scope.get("selection_sha256"),
        "authority_projection_context_sha256": lineage.get("context_input_sha256"),
        "authority_projection_code_sha256": lineage.get("projection_code_sha256"),
        "authority_projection_audit_summary_sha256": lineage.get("audit_summary_sha256"),
        "authority_projection_human_audit_sha256": lineage.get("human_audit_sha256"),
    }
    hashes: Dict[str, str] = {}
    for name, raw in digest_names.items():
        if raw not in (None, ""):
            hashes[name] = _authority_projection_sha256(raw)
    return rows, hashes


def _synthetic_authority_projection_audit(
    selected: Sequence[Tuple[Mapping[str, Any], Tuple[str, ...]]],
    *,
    selection_sha256: str,
) -> Mapping[str, Any]:
    """Build a body-free audit envelope for synthetic contract tests only."""

    # Synthetic evidence is still constrained to the exact current mapping;
    # it is not a way to mint a second five-page authority set.  The source
    # rows are validated before constructing the sidecar so an arbitrary
    # caller page list fails before a ledger can be opened.
    _authority_bound_source_mapping(selected)
    rows = []
    for index, expected in enumerate(AUTHORITY_BOUND_CANONICAL_MAPPING, 1):
        rows.append(
            {
                "selection_rank": int(expected["selection_rank"]),
                "page_ref": str(expected["authority_page_ref"]),
                "root_ref": str(expected["authority_root_ref"]),
                "source_ref": str(expected["authority_source_ref"]),
                "scope_ref": str(expected["authority_scope_ref"]),
                "authority_bound_evidence": {"all_primary_authority_bound": True},
            }
        )
    return {
        "schema": "compact_stage_a_authority_bound_primary_projection_audit_v2",
        "audit_status": "pass_exact_underprojection_replay",
        "body_free": True,
        "gate": True,
        "acceptance": {
            "status": "pass",
            "authority_bound_evidence_gate": True,
            "difference_binding_gate": True,
            "all_scopes_bound": True,
        },
        "execution_boundaries": {
            "frozen_read": False,
            "frozen_test_read": False,
            "gold_loaded": False,
            "production_state_written": False,
            "provider_called": False,
            "source_artifact_written": False,
            "provider_call_count": 0,
        },
        "input_lineage": {
            "selection_sha256": str(selection_sha256),
            "projection_code_sha256": AUTHORITY_BOUND_PROJECTION_CODE_SHA256,
        },
        "selection_scope": {
            "candidate_only": True,
            "development_only": True,
            "not_canonical": True,
            "semantic_decision_pending": True,
            "single_scope": True,
            "scope_ref": AUTHORITY_BOUND_SCOPE_REF,
            "same_five_page_materialize": True,
            "selection_binding_verified": True,
            "selected_page_count": AUTHORITY_BOUND_MAX_SELECTED_PAGES,
            "page_refs": [row["page_ref"] for row in rows],
        },
        "pages": rows,
    }


def _read_authority_projection_evidence(
    root: Optional[Union[str, Path]],
    *,
    selected: Sequence[Tuple[Mapping[str, Any], Tuple[str, ...]]],
    source_selection_sha256: str,
    summary_override: Optional[Mapping[str, Any]] = None,
    hash_override: Optional[Mapping[str, Any]] = None,
) -> Tuple[Mapping[str, Any], Tuple[Mapping[str, Any], ...], Dict[str, str]]:
    """Read/validate body-free authority projection evidence."""

    if summary_override is not None:
        summary: Mapping[str, Any] = dict(summary_override)
        audit_sha = stable_hash(summary)
    elif root is None:
        summary = _synthetic_authority_projection_audit(selected, selection_sha256=source_selection_sha256)
        audit_sha = stable_hash(summary)
    else:
        audit_root = _safe_path(root, "authority_projection_invalid")
        if audit_root.suffix.casefold() == ".json":
            path = audit_root
        else:
            path = audit_root / AUTHORITY_BOUND_AUDIT_FILENAME
        if not path.is_file():
            raise CompactStageADevelopmentError("authority_projection_invalid")
        summary = _json_read(path, "authority_projection_invalid")
        audit_sha = _file_sha256(path)
        audit_sha = _authority_projection_sha256(audit_sha, "authority_projection_invalid")
    rows, hashes = _validate_authority_projection_audit(summary)
    lineage = summary.get("input_lineage") if isinstance(summary.get("input_lineage"), Mapping) else {}
    # Preserve every opaque SHA-256 in the reviewed projection lineage (with
    # both the original key and an authority-prefixed alias).  This keeps
    # source/context/audit hash drift fail-closed without persisting the
    # sidecar's private content.
    for raw_key, raw_value in lineage.items():
        key = str(raw_key)
        if not key.casefold().endswith("_sha256") or raw_value in (None, ""):
            continue
        digest = _authority_projection_sha256(raw_value)
        hashes.setdefault(key, digest)
        hashes.setdefault("authority_projection_" + key, digest)
    audited_selection = str(lineage.get("selection_sha256") or "")
    if source_selection_sha256 and audited_selection and audited_selection != source_selection_sha256:
        raise CompactStageADevelopmentError("authority_projection_page_drift")
    if not audited_selection and source_selection_sha256:
        hashes["authority_projection_selection_sha256"] = _authority_projection_sha256(source_selection_sha256)
    hashes["authority_projection_audit_sha256"] = audit_sha
    # ``hash_override`` is intentionally ignored as an input to the binding.
    # The runner checks it later, after it has recomputed all fallback lineage
    # hashes from the actual selected input and sidecar.  In particular, this
    # prevents a caller from making a forged value authoritative merely by
    # passing it through this compatibility parameter.
    hashes["authority_projection_binding_sha256"] = stable_hash(
        {
            "audit_pages": [dict(row) for row in rows],
            "runner_pages": [
                {
                    "selection_rank": int(page.get("_selection_rank", index + 1)),
                    "page_id": str(page.get("page_id") or ""),
                    "root_id": str(page.get("root_id") or ""),
                    "source_handle": str(page.get("_source_handle") or page.get("source_packet_id") or ""),
                    "scope": _scope(page.get("scope")),
                }
                for index, (page, _categories) in enumerate(selected)
            ],
            "selection_sha256": str(source_selection_sha256 or audited_selection),
        }
    )
    return summary, rows, hashes


def _authority_bound_validate_frozen_boundary(
    *,
    stratified_mode: bool,
    lineage: Mapping[str, Any],
    authority_projection_hashes: Mapping[str, str],
    settings_sha256: str,
    scope: Mapping[str, str],
    authority_bound_code_sha256: str,
) -> None:
    """Enforce the frozen authority inputs before ledger reservation.

    ``lineage`` and ``authority_projection_hashes`` are populated from the
    runner's actual selected rows and the body-free sidecar.  This check does
    not accept a caller's digest as a source of truth; it only compares
    caller assertions after those values have been recomputed.
    """

    if _authority_bound_canonical_mapping_hash() != AUTHORITY_BOUND_CANONICAL_MAPPING_SHA256:
        raise CompactStageADevelopmentError("authority_projection_code_drift")
    if str(authority_bound_code_sha256) != AUTHORITY_BOUND_CODE_SHA256:
        raise CompactStageADevelopmentError("authority_projection_code_drift")
    if stable_hash(AUTHORITY_BOUND_SYSTEM_PROMPT) != AUTHORITY_BOUND_PROMPT_SHA256:
        raise CompactStageADevelopmentError("authority_projection_code_drift")
    try:
        normalized_scope = _scope(scope)
    except CompactStageADevelopmentError as exc:
        raise CompactStageADevelopmentError("authority_projection_scope_drift") from exc
    if normalized_scope != dict(AUTHORITY_BOUND_SCOPE):
        raise CompactStageADevelopmentError("authority_projection_scope_drift")
    if stable_hash(normalized_scope) != AUTHORITY_BOUND_SCOPE_SHA256:
        raise CompactStageADevelopmentError("authority_projection_scope_drift")
    if str(settings_sha256).strip().lower() != AUTHORITY_BOUND_SETTINGS_SHA256:
        raise CompactStageADevelopmentError("authority_projection_hash_mismatch")

    # The projection's implementation hash is a frozen sidecar input in both
    # synthetic and current-input preparation.  Missing and mismatched values
    # are distinct from ordinary page drift so audit consumers can classify
    # the failure without seeing any private content.
    projection_code = str(authority_projection_hashes.get("authority_projection_code_sha256") or "")
    if not projection_code:
        raise CompactStageADevelopmentError("authority_projection_hash_missing")
    if projection_code != AUTHORITY_BOUND_PROJECTION_CODE_SHA256:
        raise CompactStageADevelopmentError("authority_projection_code_drift")

    source_names = (
        ("selection_sha256", "authority_projection_selection_sha256"),
        ("context_input_sha256", "authority_projection_context_sha256"),
        ("audit_summary_sha256", "authority_projection_audit_summary_sha256"),
        ("human_audit_sha256", "authority_projection_human_audit_sha256"),
    )
    for source_name, projection_name in source_names:
        source_digest = str(lineage.get(source_name) or "").strip().lower()
        projection_digest = str(authority_projection_hashes.get(projection_name) or "").strip().lower()
        if not source_digest or not projection_digest:
            raise CompactStageADevelopmentError("authority_projection_hash_missing")
        _authority_projection_sha256(source_digest)
        _authority_projection_sha256(projection_digest)
        if source_digest != projection_digest:
            raise CompactStageADevelopmentError("authority_projection_hash_mismatch")

    if stratified_mode:
        frozen_lineage = (
            ("selection_sha256", AUTHORITY_BOUND_SELECTION_SHA256),
            ("context_input_sha256", AUTHORITY_BOUND_CONTEXT_INPUT_SHA256),
            ("audit_summary_sha256", AUTHORITY_BOUND_AUDIT_SUMMARY_SHA256),
            ("human_audit_sha256", AUTHORITY_BOUND_HUMAN_AUDIT_SHA256),
        )
        for name, expected in frozen_lineage:
            actual = str(lineage.get(name) or "").strip().lower()
            if actual != expected:
                raise CompactStageADevelopmentError("authority_projection_hash_mismatch")
        for projection_name, expected in (
            ("authority_projection_selection_sha256", AUTHORITY_BOUND_SELECTION_SHA256),
            ("authority_projection_context_sha256", AUTHORITY_BOUND_CONTEXT_INPUT_SHA256),
            ("authority_projection_audit_summary_sha256", AUTHORITY_BOUND_AUDIT_SUMMARY_SHA256),
            ("authority_projection_human_audit_sha256", AUTHORITY_BOUND_HUMAN_AUDIT_SHA256),
        ):
            if str(authority_projection_hashes.get(projection_name) or "").strip().lower() != expected:
                raise CompactStageADevelopmentError("authority_projection_hash_mismatch")


def _guard_subset_page_ref(page: Mapping[str, Any], index: int = 0) -> Tuple[int, str, str, str, str]:
    """Return the opaque identity tuple used to bind a guard page."""

    return (
        int(page.get("_selection_rank", page.get("selection_rank", index + 1)) or 0),
        str(page.get("page_id") or page.get("page_ref") or page.get("page_handle") or ""),
        str(page.get("root_id") or page.get("root_ref") or page.get("root_handle") or ""),
        str(page.get("_source_handle") or page.get("source_ref") or page.get("source_handle") or page.get("source_packet_id") or ""),
        str(page.get("_scope_handle") or page.get("scope_ref") or page.get("scope_handle") or ""),
    )


def _guard_selection_report(
    selected: Sequence[Tuple[Mapping[str, Any], Tuple[str, ...]]],
    subset: Sequence[Mapping[str, Any]],
    *,
    source_selection_sha256: str = "",
) -> Dict[str, Any]:
    """Build a body-free report for the exact audited guard subset."""

    rows = []
    for (page, categories), item in zip(selected, subset):
        rows.append(
            {
                "selection_rank": int(item["selection_rank"]),
                "page_ref": str(item["page_ref"]),
                "root_ref": str(item["root_ref"]),
                "source_ref": str(item["source_ref"]),
                "scope_ref": str(item["scope_ref"]),
                "cue_family": str(item["cue_family"]),
                "guard_id": str(item["guard_id"]),
                "expected_error_code": str(item.get("expected_error_code", "")),
                "expected_category": str(item.get("expected_category", "")),
                "page_hash": str(page.get("page_hash", "")),
                "categories": list(categories),
                "candidate_only": bool(page.get("_candidate_only", False)),
                "not_canonical": bool(page.get("_not_canonical", False)),
                "semantic_decision_pending": bool(page.get("_semantic_decision_pending", False)),
            }
        )
    return {
        "schema": "compact_stage_a_guard_validation_selection_v1",
        "selection_rule": "explicit_audited_subset_only",
        "guard_validation": True,
        "body_free": True,
        "selected": rows,
        "selected_page_count": len(rows),
        "selected_page_limit": GUARD_VALIDATION_MAX_SELECTED_PAGES,
        "source_selection_sha256": str(source_selection_sha256 or ""),
        "subset_sha256": _guard_subset_hash(subset),
    }


def _mapping(value: Any, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CompactStageADevelopmentError(code)
    return value


def _string(value: Any, code: str, *, allow_empty: bool = True, max_length: int = 512) -> str:
    if type(value) is not str or len(value) > max_length or (not allow_empty and not value) or value != value.strip():
        raise CompactStageADevelopmentError(code)
    return value


def _scope(value: Any) -> Dict[str, str]:
    if isinstance(value, Mapping):
        account = value.get("account_id", value.get("account", value.get("a")))
        chat = value.get("chat_id", value.get("chat", value.get("c")))
    elif isinstance(value, str) and "/" in value:
        account, chat = value.split("/", 1)
    else:
        raise CompactStageADevelopmentError("scope_invalid")
    return {
        "account_id": _string(account, "scope_invalid", allow_empty=False, max_length=128),
        "chat_id": _string(chat, "scope_invalid", allow_empty=False, max_length=256),
    }


def _scope_pair(value: Mapping[str, Any]) -> Tuple[str, str]:
    normalized = _scope(value)
    return normalized["account_id"], normalized["chat_id"]


def _rows(value: Any) -> List[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [child for child in value.values() if isinstance(child, Mapping)]
    if isinstance(value, (list, tuple)):
        return [child for child in value if isinstance(child, Mapping)]
    return []


def _id_from_row(row: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return str(value)
    return ""


def _table(store: Optional[Mapping[str, Any]], singular: str, plural: str) -> Dict[str, Mapping[str, Any]]:
    if not isinstance(store, Mapping):
        return {}
    value = store.get(singular)
    if value is None:
        value = store.get(plural)
    if isinstance(value, Mapping):
        return {str(key): child for key, child in value.items() if isinstance(child, Mapping)}
    if isinstance(value, (list, tuple)):
        output: Dict[str, Mapping[str, Any]] = {}
        for row in value:
            if not isinstance(row, Mapping):
                continue
            handle = _id_from_row(row, "message_handle", "candidate_handle", "evidence_handle", "handle", "id")
            if handle:
                output[handle] = row
        return output
    return {}


def _find_store_row(store: Optional[Mapping[str, Any]], handle: str, kind: str) -> Mapping[str, Any]:
    singular = {"m": "message_table", "c": "candidate_table", "e": "evidence_table"}.get(kind, "message_table")
    plural = {"m": "messages", "c": "candidates", "e": "evidence"}.get(kind, "messages")
    table = _table(store, singular, plural)
    if handle in table:
        return table[handle]
    for row in table.values():
        if _id_from_row(row, "message_handle", "candidate_handle", "evidence_handle", "handle") == handle:
            return row
    return {}


def _message_identity_refs(value: Any) -> set[str]:
    """Return opaque message ids/aliases without traversing body fields."""

    if not isinstance(value, Mapping):
        return set()
    refs: set[str] = set()
    for key in (
        "message_id",
        "source_message_id",
        "message_handle",
        "source_message_handle",
        "message_alias",
        "source_message_alias",
        "alias",
        "handle",
        "id",
    ):
        marker = value.get(key)
        if marker not in (None, "") and not isinstance(marker, (Mapping, list, tuple, set, frozenset)):
            refs.add(str(marker))
    for key in ("message_ids", "source_message_ids", "message_handles", "message_aliases", "aliases", "handles"):
        markers = value.get(key)
        if isinstance(markers, (list, tuple, set, frozenset)):
            refs.update(str(marker) for marker in markers if marker not in (None, ""))
        elif isinstance(markers, str) and markers:
            refs.add(markers)
    # Only role/authority wrappers are followed.  Arbitrary nested content is
    # intentionally not searched because a body value must never become an
    # authoritative message binding by accident.
    for key in _AUTHORITY_METADATA_KEYS:
        nested = value.get(key)
        if isinstance(nested, Mapping):
            refs.update(_message_identity_refs(nested))
        elif isinstance(nested, (list, tuple)):
            for item in nested:
                refs.update(_message_identity_refs(item))
    return refs


def _authority_rows_from(value: Any) -> List[Mapping[str, Any]]:
    """Extract explicitly named authority rows from a page/root/store row."""

    if not isinstance(value, Mapping):
        return []
    result: List[Mapping[str, Any]] = []

    def add(child: Any, *, parent_ref: Optional[str] = None) -> None:
        if isinstance(child, Mapping):
            refs = _message_identity_refs(child)
            if refs or parent_ref:
                if parent_ref and not refs:
                    clone = dict(child)
                    clone["message_handle"] = parent_ref
                    result.append(clone)
                else:
                    result.append(child)
            else:
                for nested_key, nested in child.items():
                    add(nested, parent_ref=str(nested_key))
        elif isinstance(child, (list, tuple, set, frozenset)):
            for item in child:
                add(item, parent_ref=parent_ref)

    for raw_key, child in value.items():
        key = str(raw_key).casefold()
        if key in _AUTHORITY_METADATA_KEYS:
            add(child)
        elif key in {"layer_rows", "layers", "role_layers"} and isinstance(child, Mapping):
            for nested_key, nested in child.items():
                if str(nested_key).casefold() in _AUTHORITY_METADATA_KEYS:
                    add(nested)
    # A central linear message record stores immutable rows under these
    # fields even though the enclosing record is named ``messages``.
    for key in ("authority_rows", "identity_row", "authoritative", "authority"):
        child = value.get(key)
        if isinstance(child, Mapping):
            add(child)
        elif isinstance(child, (list, tuple)):
            add(child)
    # A table mapping is keyed by opaque handles rather than an authority
    # field name.  Treat only value rows that expose an id/alias as candidates
    # so arbitrary body dictionaries cannot become metadata rows.
    if not result and value and all(isinstance(child, Mapping) for child in value.values()):
        for child in value.values():
            if _message_identity_refs(child):
                add(child)
    return result


def _scope_matches(value: Mapping[str, Any], scope: Mapping[str, str]) -> bool:
    """Check explicit account/chat markers without inferring from opaque ids."""

    if not isinstance(value, Mapping):
        return True
    observed_account = value.get("account_id", value.get("account"))
    observed_chat = value.get("chat_id", value.get("chat"))
    nested = value.get("scope")
    if isinstance(nested, Mapping):
        observed_account = observed_account or nested.get("account_id", nested.get("account"))
        observed_chat = observed_chat or nested.get("chat_id", nested.get("chat"))
    if observed_account not in (None, "", "unknown", scope.get("account_id")):
        return False
    if observed_chat not in (None, "", "unknown", scope.get("chat_id")):
        return False
    return True


def _direct_human_cue_from_sources(*sources: Mapping[str, Any]) -> str:
    """Return the first direct caption/text cue, never a merged candidate."""

    for source in sources:
        if not isinstance(source, Mapping):
            continue
        # ``_merge_context_packet_bodies`` may overlay a K2 fragment cue on
        # the linear identity row.  Treat that overlay as candidate/context
        # material for typed media; only an untouched direct caption/text
        # field may cross the authority barrier.
        source_for_cue: Mapping[str, Any] = source
        # The context adapter may explicitly mark one source-primary event
        # fragment as a direct human caption.  That narrow exception is
        # allowed to retain its overlaid cue; merged candidate material still
        # takes the stricter path below and is always stripped.
        overlay_caption = (
            source.get("_context_direct_caption") is True
            and not _has_merged_candidate_cue(source)
        )
        if (
            source.get("_context_body_overlay") is True
            and not overlay_caption
        ) or _has_merged_candidate_cue(source):
            source_for_cue = {
                key: value
                for key, value in source.items()
                if key not in {"text", "text_redacted", "message_text"}
            }
        cue = _explicit_human_cue(source_for_cue)
        if cue:
            return cue
        for key in ("identity_row", "authority", "authoritative", "message_metadata"):
            nested = source.get(key)
            if isinstance(nested, Mapping):
                nested_for_cue: Mapping[str, Any] = nested
                # Candidate/activation markers can be carried by the
                # immutable identity wrapper rather than the flattened row.
                # Apply the same authority barrier to that wrapper so its
                # overlaid text cannot become an independent provider cue.
                nested_overlay_caption = (
                    nested.get("_context_direct_caption") is True
                    and not _has_merged_candidate_cue(nested)
                )
                if (
                    nested.get("_context_body_overlay") is True
                    and not nested_overlay_caption
                ) or _has_merged_candidate_cue(nested):
                    nested_for_cue = {
                        nested_key: nested_value
                        for nested_key, nested_value in nested.items()
                        if nested_key not in {"text", "text_redacted", "message_text"}
                    }
                cue = _explicit_human_cue(nested_for_cue)
                if cue:
                    return cue
    return ""


def _has_merged_candidate_cue(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    for key, child in value.items():
        if str(key).casefold() in _MERGED_CUE_KEYS:
            # The adapter deliberately carries only a boolean marker for a
            # merged candidate cue.  Treat an explicit false marker as
            # absent; otherwise a body-free ``*_present`` field would make
            # every row look merged merely because the key exists.
            if child is True or child == 1:
                return True
            if child not in (None, "", [], (), {}, False, 0):
                return True
    # Message-table rows keep the immutable identity under ``identity_row``;
    # marker propagation can therefore be nested even when the linear merge
    # has not flattened the row yet.  Follow only identity/authority wrappers
    # so arbitrary body/content dictionaries cannot become a cue marker.
    for key in ("identity_row", "source_row", "mapping_row", "authoritative", "authority"):
        nested = value.get(key)
        if isinstance(nested, Mapping) and _has_merged_candidate_cue(nested):
            return True
    return False


def _authority_content_presence(value: Mapping[str, Any]) -> Tuple[bool, bool]:
    """Return (any content, direct caption) markers without returning body."""

    if not isinstance(value, Mapping):
        return False, False
    direct = False
    content = False
    for key in ("content", "body", "text", "text_redacted", "message_text", "caption", "description"):
        marker = value.get(key)
        if isinstance(marker, str) and marker.strip():
            content = True
            if key in _DIRECT_HUMAN_CUE_FIELDS:
                direct = True
    for key in ("identity_row", "authority", "authoritative", "message_metadata"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            nested_content, nested_direct = _authority_content_presence(nested)
            content = content or nested_content
            direct = direct or nested_direct
    return content, direct


def _evidence_span_from_sources(
    row: Mapping[str, Any],
    authority: Mapping[str, Any],
    page: Mapping[str, Any],
    root: Mapping[str, Any],
    store: Optional[Mapping[str, Any]],
) -> bool:
    """Find a concrete bounded evidence span for one message binding."""

    def valid(span: Any) -> bool:
        if not isinstance(span, Mapping):
            return False
        start = span.get("start", span.get("span_start"))
        end = span.get("end", span.get("span_end", start))
        return isinstance(start, int) and isinstance(end, int) and start >= 0 and end >= start

    def scan(value: Any) -> bool:
        if isinstance(value, Mapping):
            if valid(value.get("span")):
                return True
            for key in ("evidence_refs", "evidence", "evidence_rows"):
                child = value.get(key)
                if isinstance(child, (list, tuple)) and any(scan(item) for item in child):
                    return True
            return False
        if isinstance(value, (list, tuple)):
            return any(scan(item) for item in value)
        return False

    for source in (row, authority):
        if scan(source):
            return True
    message_refs = _message_identity_refs(row) | _message_identity_refs(authority)
    evidence_table = _table(store, "evidence_table", "evidence")
    for key, evidence in evidence_table.items():
        if not isinstance(evidence, Mapping):
            continue
        evidence_refs = _message_identity_refs(evidence)
        if message_refs.intersection(evidence_refs) and scan(evidence):
            return True
    for source in (page, root):
        for key in ("evidence_refs", "evidence", "evidence_rows"):
            values = source.get(key)
            if isinstance(values, (list, tuple)):
                for evidence in values:
                    if isinstance(evidence, Mapping) and message_refs.intersection(_message_identity_refs(evidence)) and scan(evidence):
                        return True
    return False


def _authority_metadata_markers(
    *sources: Mapping[str, Any],
) -> Tuple[bool, bool]:
    """Return whether role and message-type metadata are explicitly present.

    Message-table records commonly keep the immutable metadata in an
    ``identity_row`` wrapper.  This helper follows only those wrappers and
    never searches arbitrary content/body values.  The booleans are used as a
    provider-boundary gate; they are intentionally safe to expose in
    body-free telemetry.
    """

    role_present = False
    type_present = False
    role_keys = (
        "role",
        "roles",
        "layer",
        "message_role",
        "dialogue_role",
        "semantic_role",
        "provider_role",
        "primary_context_role",
    )
    type_keys = ("message_type", "type", "media_type", "content_type")

    def visit(source: Mapping[str, Any], seen: set[int]) -> None:
        nonlocal role_present, type_present
        if not isinstance(source, Mapping) or id(source) in seen:
            return
        seen.add(id(source))
        for key in role_keys:
            value = source.get(key)
            if isinstance(value, (list, tuple, set, frozenset)):
                if any(str(item or "").strip() and str(item).strip().casefold() != "unknown" for item in value):
                    role_present = True
            elif value not in (None, "") and str(value).strip().casefold() != "unknown":
                role_present = True
        for key in type_keys:
            value = source.get(key)
            if value not in (None, "") and str(value).strip().casefold() != "unknown":
                type_present = True
        for key in ("identity_row", "source_row", "mapping_row", "authoritative", "authority", "message_metadata"):
            nested = source.get(key)
            if isinstance(nested, Mapping):
                visit(nested, seen)

    seen: set[int] = set()
    for source in sources:
        visit(source, seen)
    return role_present, type_present


def _authoritative_direct_cue(
    row: Mapping[str, Any],
    authority: Mapping[str, Any],
    text: str = "",
) -> str:
    """Resolve a direct provider cue while excluding merged candidates.

    A normal K2 fragment body is overlaid onto its immutable message row in
    memory and is valid when it is not marked as a merged candidate.  This is
    distinct from a candidate/activation cue, whose marker must never make an
    otherwise unbound row provider-primary.
    """

    direct = _direct_human_cue_from_sources(row, authority)
    if direct:
        return direct
    if (
        text
        and isinstance(row, Mapping)
        and not _has_merged_candidate_cue(row)
        and not _projection_record_is_provider_media(row)
        and not _projection_record_is_provider_media(authority)
    ):
        cue = " ".join(str(text).split())
        if _semantic_cue_is_eligible(cue):
            return cue[:128]
    return ""


def _authoritative_message_metadata(
    row: Mapping[str, Any],
    handle: str,
    page: Mapping[str, Any],
    root: Mapping[str, Any],
    store: Optional[Mapping[str, Any]],
    *,
    text: str = "",
) -> Dict[str, Any]:
    """Resolve one message's authoritative metadata and binding evidence.

    The result is an in-memory projection only.  It carries the source row so
    role/type/caption gates can inspect it, but callers persist only booleans,
    counts and hashes from the resulting decision.
    """

    target_refs = _message_identity_refs(row)
    if handle:
        target_refs.add(str(handle))
    if not target_refs:
        target_refs.add(str(handle or ""))
    candidates: List[Mapping[str, Any]] = []
    seen: set[int] = set()
    scope_conflict = False

    def add_source(source: Any) -> None:
        nonlocal scope_conflict
        for candidate in _authority_rows_from(source):
            if id(candidate) in seen:
                continue
            if not _scope_matches(candidate, page.get("scope", {})):
                if _message_identity_refs(candidate).intersection(target_refs):
                    scope_conflict = True
                continue
            seen.add(id(candidate))
            candidates.append(candidate)

    add_source(row)
    add_source(page)
    add_source(root)
    materialized = page.get("_materialized_meta")
    add_source(materialized)
    if isinstance(store, Mapping):
        # Facts/authority tables are explicit sources.  The message table is
        # inspected only through its nested identity/authority rows.
        for singular, plural in (
            ("authoritative_fact_table", "authoritative_facts"),
            ("authority_table", "authority"),
            ("message_metadata", "message_metadata"),
            ("messages", "messages"),
            ("message_table", "message_table"),
        ):
            add_source(_table(store, singular, plural))
        add_source(store.get("layer_rows"))

    matching: List[Mapping[str, Any]] = []
    for candidate in candidates:
        refs = _message_identity_refs(candidate)
        if refs.intersection(target_refs):
            matching.append(candidate)
    authority: Mapping[str, Any]
    explicit = bool(matching)
    if matching:
        # Prefer a row carrying ``metadata_authoritative`` and otherwise keep
        # the first deterministic source row.  No candidate row participates.
        authority = next(
            (candidate for candidate in matching if candidate.get("metadata_authoritative") is True),
            matching[0],
        )
    else:
        authority = {}

    binding = _source_primary_binding(page, root)
    row_refs = set(target_refs)
    row_refs.update(_row_binding_refs(row))
    # Resolve source membership against both the row's aliases and the
    # matching authoritative row.  A page may bind only the canonical
    # ``message_id`` while the provider row is addressed by a message alias.
    authority_refs = _message_identity_refs(authority)
    source_roles = _source_role_groups(binding, row_refs | authority_refs)
    # Compatibility for direct synthetic mappings and for linear message
    # rows whose immutable identity is the only available authority handle:
    # a source-bound message row is an implicit authority row.  Unbound
    # residual/candidate rows do not enter this branch and remain context.
    if (
        not authority
        and not scope_conflict
        and source_roles
        and not _has_merged_candidate_cue(row)
        and (_authority_content_presence(row)[0] or _role_label_sets(_role_sources(row))[0] or _role_label_sets(_role_sources(row))[1])
    ):
        authority = row
        explicit = False
        # The compatibility authority is the row itself, so its identity is
        # now the authoritative alias set for the remaining gate checks.
        authority_refs = _message_identity_refs(authority)

    canonical_id = _id_from_row(authority, "message_id", "source_message_id", "message_alias", "source_message_alias", "message_handle", "source_message_handle", "alias", "handle") if authority else ""
    if not canonical_id:
        canonical_id = _id_from_row(row, "message_id", "source_message_id", "message_alias", "source_message_alias", "message_handle", "source_message_handle", "alias", "handle")
    aliases = set(target_refs)
    aliases.update(authority_refs)
    metadata_role_present, metadata_type_present = _authority_metadata_markers(authority, row)
    direct_cue = _authoritative_direct_cue(row, authority, text)
    # Legacy synthetic/linear rows can carry their source role and direct cue
    # without repeating a separate metadata wrapper.  Treat that bounded
    # source role as the role marker and ordinary text as ``type=text``; the
    # compatibility inference is deliberately unavailable to media rows or
    # merged candidate cues.
    if not metadata_role_present and source_roles:
        metadata_role_present = True
    if not metadata_type_present and direct_cue and not _projection_record_is_provider_media(row) and not _projection_record_is_provider_media(authority):
        metadata_type_present = True
    metadata_gate = bool(metadata_role_present and metadata_type_present and direct_cue)
    authoritative_identity_bound = bool(authority_refs.intersection(target_refs)) if authority else False
    authoritative_bound = bool(
        authority
        and canonical_id
        and canonical_id.casefold() != "unknown"
        and source_roles
        and authoritative_identity_bound
        and metadata_gate
        and not scope_conflict
    )
    content_present, _ = _authority_content_presence(authority or row)
    # ``_authority_content_presence`` is intentionally broad and counts an
    # overlaid/merged body as content.  The provider gate needs the narrower
    # direct-caption signal instead; a body-free adapter may also carry the
    # scalar marker when it observed a direct caption in its source row.
    direct_caption_present = bool(direct_cue)
    if not _has_merged_candidate_cue(row):
        direct_caption_present = direct_caption_present or any(
            source.get("direct_caption_present") is True or source.get("direct_caption_present") == 1
            for source in (row, authority)
            if isinstance(source, Mapping)
        )
    evidence_span = _evidence_span_from_sources(row, authority or row, page, root, store)
    # A direct source-primary text row is itself an auditable bounded unit in
    # older synthetic linear maps that do not repeat the evidence table.
    if not evidence_span and source_roles and set(source_roles).intersection({"primary", "context"}) and not _has_merged_candidate_cue(row) and bool(direct_cue):
        evidence_span = True
    return {
        "metadata": authority,
        "explicit": bool(explicit),
        "scope_valid": not scope_conflict,
        "canonical_id": canonical_id,
        "aliases": frozenset(aliases),
        "source_roles": tuple(source_roles),
        "bound": bool(authoritative_bound),
        "authoritative_message_id_bound": bool(authoritative_identity_bound and canonical_id),
        "authoritative_alias_bound": bool(authoritative_identity_bound),
        "metadata_role_present": bool(metadata_role_present),
        "metadata_type_present": bool(metadata_type_present),
        "metadata_direct_caption_present": bool(direct_cue),
        "metadata_gate": bool(metadata_gate),
        "content_present": bool(content_present),
        "direct_caption_present": bool(direct_caption_present),
        "evidence_span": bool(evidence_span),
    }


def _extract_text(row: Mapping[str, Any], store: Optional[Mapping[str, Any]]) -> str:
    """Extract a bounded in-memory cue; never returned in an artifact."""

    # Typed media/system rows and authority/placeholder roles may carry XML,
    # JSON, transport payloads, or descriptive metadata in ``content``/
    # ``description``.  Only a direct human caption/text field is eligible at
    # the provider boundary; do not fall through to nested content for these
    # rows.  This keeps an empty authority context-only even when metadata is
    # non-empty.
    if _projection_record_is_provider_media(row):
        return _direct_human_cue_from_sources(row)
    # A merged candidate/activation cue is not a message caption.  Preserve a
    # separately carried direct caption, but never expose the merged text as
    # the provider registration cue.
    if _has_merged_candidate_cue(row):
        return _direct_human_cue_from_sources(row)

    values: List[Any] = []
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
        "identity_row",
    ):
        if key in row:
            values.append(row.get(key))
    handles = row.get("content_handles")
    content_table = store.get("content_table") if isinstance(store, Mapping) else None
    if isinstance(handles, (list, tuple)) and isinstance(content_table, Mapping):
        for handle in handles:
            content = content_table.get(str(handle))
            if isinstance(content, Mapping):
                values.extend(
                    content.get(key)
                    for key in ("body", "text", "content", "material", "caption", "alt_text", "description")
                    if key in content
                )
    def flatten(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, Mapping):
            for key in (
                "text",
                "body",
                "content",
                "material",
                "message_text",
                "text_redacted",
                "caption",
                "alt_text",
                "description",
            ):
                if isinstance(value.get(key), str):
                    return str(value[key])
            for child in value.values():
                nested = flatten(child)
                if nested:
                    return nested
        if isinstance(value, (list, tuple)):
            return " ".join(filter(None, (flatten(child) for child in value)))
        return ""
    for value in values:
        text = " ".join(flatten(value).split())
        if text:
            return text[:128]
    return ""


# These fields are the body-free role binding emitted by the linear source and
# its materialized map.  A provider alias is allowed to be ``primary`` only
# when its message handle or message id is present in the first group.  The
# other groups are retained for an explicit context-only decision; a cue must
# never promote one of them.
_SOURCE_PRIMARY_FIELDS = frozenset(
    {
        "source_primary_message_ids",
        "source_primary_message_handles",
        "source_primary_ids",
        "source_primary_handles",
        "primary_message_ids",
        "primary_message_handles",
        "primary_ids",
        "primary_handles",
    }
)
_SOURCE_AUTHORITY_FIELDS = frozenset(
    {
        "source_authority_message_ids",
        "source_authority_message_handles",
        "authority_message_ids",
        "authority_message_handles",
        "authority_ids",
        "authority_handles",
    }
)
_SOURCE_CONTEXT_FIELDS = frozenset(
    {
        "adjacent_message_ids",
        "adjacent_message_handles",
        "context_message_ids",
        "context_message_handles",
        "context_ids",
        "context_handles",
    }
)
_SOURCE_CANDIDATE_FIELDS = frozenset(
    {
        "candidate_message_ids",
        "candidate_message_handles",
        "candidate_ids",
        "candidate_handles",
    }
)
_SOURCE_PRIMARY_ROW_FIELDS = frozenset(
    {
        "source_primary",
        "source_primary_rows",
        "source_primary_fragments",
        "primary_rows",
        "primary_fragments",
    }
)
_SOURCE_AUTHORITY_ROW_FIELDS = frozenset(
    {
        "source_authority",
        "source_authority_rows",
        "authority_rows",
        "authoritative_facts",
    }
)
_SOURCE_CONTEXT_ROW_FIELDS = frozenset(
    {
        "context_rows",
        "context_fragments",
        "adjacent_rows",
        "adjacent_context",
    }
)
_SOURCE_CANDIDATE_ROW_FIELDS = frozenset(
    {
        "candidate_rows",
        "candidate_messages",
        "candidate_fragments",
    }
)
_SOURCE_BINDING_ID_FIELDS = (
    "message_handle",
    "message_id",
    "source_message_id",
    "source_message_handle",
    "handle",
    "id",
    "fragment_id",
)


def _binding_refs(value: Any) -> Iterable[str]:
    """Yield only opaque handle/id-like values from role metadata."""

    if isinstance(value, str):
        if value:
            yield value
        return
    if isinstance(value, Mapping):
        emitted = False
        for key in _SOURCE_BINDING_ID_FIELDS:
            if key in value:
                emitted = True
                yield from _binding_refs(value.get(key))
        # Materialized maps occasionally wrap an id list in ``ids`` or
        # ``handles``.  Keep this narrow so an arbitrary body-bearing field
        # cannot become a role reference.
        if not emitted:
            for key in ("ids", "handles", "values", "members"):
                if key in value:
                    yield from _binding_refs(value.get(key))
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for child in value:
            yield from _binding_refs(child)


def _row_binding_refs(row: Mapping[str, Any]) -> set[str]:
    refs = set(_binding_refs(row))
    for key in ("identity_row", "source_row", "mapping_row"):
        child = row.get(key)
        if isinstance(child, Mapping):
            refs.update(_binding_refs(child))
    return {str(value) for value in refs if value not in (None, "")}


def _source_binding_sources(
    page: Mapping[str, Any],
    root: Mapping[str, Any],
) -> Tuple[Mapping[str, Any], ...]:
    sources: List[Mapping[str, Any]] = []
    seen: set[int] = set()
    for source in (page, root):
        if not isinstance(source, Mapping) or id(source) in seen:
            continue
        sources.append(source)
        seen.add(id(source))
        for key in ("_materialized_meta", "materialized", "_materialized"):
            child = source.get(key)
            if isinstance(child, Mapping) and id(child) not in seen:
                sources.append(child)
                seen.add(id(child))
    return tuple(sources)


def _source_primary_binding(
    page: Mapping[str, Any],
    root: Mapping[str, Any],
) -> Dict[str, Any]:
    """Collect authoritative role sets and a stable body-free set hash."""

    primary: set[str] = set()
    authority: set[str] = set()
    context: set[str] = set()
    candidate: set[str] = set()

    def add_rows(target: set[str], value: Any) -> None:
        if isinstance(value, Mapping):
            rows = _rows(value)
            if rows:
                for row in rows:
                    target.update(_row_binding_refs(row))
            else:
                target.update(_binding_refs(value))
        elif isinstance(value, (list, tuple, set, frozenset)):
            for row in value:
                if isinstance(row, Mapping):
                    target.update(_row_binding_refs(row))
                else:
                    target.update(_binding_refs(row))
        else:
            target.update(_binding_refs(value))

    for source in _source_binding_sources(page, root):
        for key, value in source.items():
            folded = str(key).casefold()
            if folded in _SOURCE_PRIMARY_FIELDS:
                add_rows(primary, value)
            elif folded in _SOURCE_AUTHORITY_FIELDS:
                add_rows(authority, value)
            elif folded in _SOURCE_CONTEXT_FIELDS:
                add_rows(context, value)
            elif folded in _SOURCE_CANDIDATE_FIELDS:
                add_rows(candidate, value)

            if folded in _SOURCE_PRIMARY_ROW_FIELDS:
                add_rows(primary, value)
            elif folded in _SOURCE_AUTHORITY_ROW_FIELDS:
                add_rows(authority, value)
            elif folded in _SOURCE_CONTEXT_ROW_FIELDS:
                add_rows(context, value)
            elif folded in _SOURCE_CANDIDATE_ROW_FIELDS:
                add_rows(candidate, value)

            # ``layer_rows`` is the current linear materialized shape.  Only
            # role-bearing child names are traversed; source refs/candidate
            # views are deliberately not treated as primary evidence.
            if folded in {"layer_rows", "layers", "role_layers"} and isinstance(value, Mapping):
                for child_key, child_value in value.items():
                    child_folded = str(child_key).casefold()
                    if child_folded in _SOURCE_PRIMARY_ROW_FIELDS:
                        add_rows(primary, child_value)
                    elif child_folded in _SOURCE_AUTHORITY_ROW_FIELDS:
                        add_rows(authority, child_value)
                    elif child_folded in _SOURCE_CONTEXT_ROW_FIELDS:
                        add_rows(context, child_value)
                    elif child_folded in _SOURCE_CANDIDATE_ROW_FIELDS:
                        add_rows(candidate, child_value)

    primary = {str(value) for value in primary if value not in (None, "")}
    authority = {str(value) for value in authority if value not in (None, "")}
    context = {str(value) for value in context if value not in (None, "")}
    candidate = {str(value) for value in candidate if value not in (None, "")}
    return {
        "primary": frozenset(primary),
        "authority": frozenset(authority),
        "context": frozenset(context),
        "candidate": frozenset(candidate),
        "provided": bool(primary),
        "sha256": stable_hash({"source_primary": sorted(primary)}),
    }


def _role_sources(row: Mapping[str, Any]) -> Tuple[Mapping[str, Any], ...]:
    nested: List[Mapping[str, Any]] = [row]
    identity = row.get("identity_row")
    if isinstance(identity, Mapping):
        nested.append(identity)
    for key in (
        "source_primary_rows",
        "primary_rows",
        "authority_rows",
        "adjacent_rows",
        "context_rows",
    ):
        nested.extend(child for child in _rows(row.get(key)) if isinstance(child, Mapping))
    return tuple(nested)


def _role_label_sets(
    sources: Sequence[Mapping[str, Any]],
) -> Tuple[set[str], set[str], set[str]]:
    semantic: set[str] = set()
    raw_roles: set[str] = set()
    role_values: set[str] = set()
    for source in sources:
        for key in ("semantic_role", "provider_role", "primary_context_role"):
            value = source.get(key)
            if value not in (None, ""):
                semantic.add(_normalise_semantic_label(value))
        for key in ("role", "layer", "message_role"):
            value = source.get(key)
            if value not in (None, ""):
                raw_roles.add(_normalise_semantic_label(value))
        values = source.get("roles")
        if isinstance(values, (list, tuple, set, frozenset)):
            role_values.update(_normalise_semantic_label(value) for value in values if value not in (None, ""))
    return semantic, raw_roles, role_values


_PROVIDER_POSITIVE_ROLES = frozenset({"primary", "substantive", "mixed", "ellipsis"})
_PROVIDER_SUBSTANTIVE_ROLES = frozenset({"substantive", "mixed"})
_PROVIDER_CONTEXT_ROLES = frozenset(
    {
        "authority",
        "authority_only",
        "empty_authority",
        "context",
        "context_only",
        "adjacent",
        "secondary",
        "candidate",
        "candidate_only",
        "conversation_opener",
        "greeting",
        "ack",
        "acknowledgement",
        "reaction",
        "media",
        "media_placeholder",
        "unknown_context",
        "unknown",
    }
)
_PROVIDER_AUTHORITY_BARRIER_ROLES = frozenset(
    {
        "media",
        "media_placeholder",
        "placeholder",
        "system",
        "event",
        "event_placeholder",
        "event_place_holder",
        "empty",
        "empty_authority",
        "reaction",
        "greeting",
        "conversation_opener",
        "ack",
        "acknowledgement",
        "context_only",
        "authority_only",
    }
)
_PROVIDER_AUTHORITY_BARRIER_TYPES = frozenset(
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
        "system",
        "location",
        "media",
        "event",
        "event_message",
        "event_placeholder",
    }
)
_DIRECT_HUMAN_CUE_FIELDS = ("caption", "text", "text_redacted", "message_text")
_AUTHORITY_METADATA_KEYS = frozenset(
    {
        "authoritative_facts",
        "message_metadata",
        "authority_rows",
        "source_authority_rows",
        "source_authority",
        "authority",
        "authorities",
        "facts",
        "identity_row",
    }
)
_MERGED_CUE_KEYS = frozenset(
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
_PROVIDER_SUBSTANTIVE_FRAGMENT_TYPES = frozenset(
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
        # A bounded lexical ellipsis is a real continuation unit when its
        # immutable authority row supplies a valid source-primary binding.
        # Punctuation-only cues remain ineligible at the semantic cue gate.
        "ellipsis",
        "short_ellipsis",
        "short_or_ellipsis",
        "ellipsis_fragment",
    }
)


def _projection_record_is_provider_media(value: Any) -> bool:
    """Apply the media/authority barrier without confusing overlap metadata.

    The linear materialization keeps source memberships in ``roles``.  A
    message that is both source-primary and authoritative therefore commonly
    carries ``["primary", "authority"]`` alongside its semantic
    ``role="substantive"``.  The protocol helper intentionally treats an
    authority label as a barrier, which is correct for a context-only
    authority row but too broad for this overlapping source row: it suppresses
    the bounded text cue before the authority binding can be evaluated.

    Keep explicit transport/media types and placeholder fragment types
    fail-closed.  Only a positive substantive/mixed/primary semantic role on a
    non-media fragment with a text-like type can neutralize the *overlap-only*
    authority label.  This helper is local to the development projection;
    the compact protocol's standalone media/authority behavior is unchanged.
    """

    if not isinstance(value, Mapping):
        return _record_is_provider_media(value)
    if not _record_is_provider_media(value):
        return False

    sources: List[Mapping[str, Any]] = [value]
    for key in ("identity_row", "source_row", "mapping_row", "authoritative", "authority"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            sources.append(nested)

    labels: set[str] = set()
    for source in sources:
        for key in ("semantic_role", "provider_role", "primary_context_role", "role", "layer", "message_role"):
            marker = _normalise_semantic_label(source.get(key))
            if marker:
                labels.add(marker)
        roles = source.get("roles")
        if isinstance(roles, (list, tuple, set, frozenset)):
            labels.update(
                _normalise_semantic_label(marker)
                for marker in roles
                if marker not in (None, "")
            )

    positive = bool(labels.intersection(_PROVIDER_POSITIVE_ROLES))
    if not positive:
        return True

    explicit_types = {
        _normalise_semantic_label(source.get(key))
        for source in sources
        for key in ("message_type", "type", "media_type", "content_type")
        if source.get(key) not in (None, "")
    }
    media_types = {
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
        "system",
        "location",
        "media",
        "event",
        "event_message",
        "event_placeholder",
        "event_place_holder",
    }
    if explicit_types.intersection(media_types):
        return True

    fragment_types = {
        _normalise_semantic_label(source.get("fragment_type", source.get("kind", "")))
        for source in sources
    }
    if fragment_types.intersection(
        {"media", "placeholder", "media_placeholder", "event_placeholder", "event_place_holder"}
    ):
        return True

    # ``roles`` is provenance in this shape; a text/statement substantive row
    # is not a media row merely because it also belongs to an authority layer.
    return not bool(explicit_types.intersection({"text", "text_message", "plain_text", "message"}) or fragment_types.intersection(_PROVIDER_SUBSTANTIVE_FRAGMENT_TYPES))


def _source_role_groups(
    binding: Mapping[str, Any],
    row_refs: set[str],
) -> Tuple[str, ...]:
    groups: List[str] = []
    for name in ("primary", "context", "authority", "candidate"):
        values = binding.get(name, ())
        if isinstance(values, (set, frozenset, list, tuple)) and set(values).intersection(row_refs):
            groups.append(name)
    return tuple(groups)


def _projected_message_role(
    row: Mapping[str, Any],
    handle: str,
    page: Mapping[str, Any],
    root: Mapping[str, Any],
    *,
    text: str = "",
    store: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Project one message into the provider primary/context channel.

    The source role remains useful provenance, but it is not the complete
    provider role.  Linear packets retain substantive turns in an adjacent
    window; those turns are promoted when their role/cue is semantic.  Pure
    social turns, authority/candidate placeholders and media placeholders
    stay context.  The returned mapping contains only role/count-friendly
    scalar metadata and never a cue/body value.
    """

    nested = _role_sources(row)
    binding = _source_primary_binding(page, root)
    row_refs = _row_binding_refs(row)
    row_refs.add(str(handle))
    authority_info = _authoritative_message_metadata(row, handle, page, root, store, text=text)
    authority = authority_info.get("metadata") if isinstance(authority_info.get("metadata"), Mapping) else {}
    authority_nested = _role_sources(authority) if isinstance(authority, Mapping) and authority else ()
    nested = tuple(nested) + tuple(authority_nested)
    semantic, raw_roles, role_values = _role_label_sets(nested)
    labels = semantic | raw_roles | role_values
    authority_semantic, authority_raw, authority_values = _role_label_sets(authority_nested)
    authority_labels = authority_semantic | authority_raw | authority_values
    row_for_signal = dict(row)
    if text and not any(row_for_signal.get(key) not in (None, "") for key in ("text", "text_redacted", "content", "body", "caption", "message_text")):
        row_for_signal["text"] = text
    merged_candidate_cue = _has_merged_candidate_cue(row)
    direct_human_cue = _authoritative_direct_cue(row, authority, text)
    signal_text = direct_human_cue if merged_candidate_cue else text
    if merged_candidate_cue and not direct_human_cue:
        signal_text = ""
        for key in ("text", "text_redacted", "message_text"):
            if key in row_for_signal:
                row_for_signal[key] = ""
    signal = _record_has_substantive_signal(row_for_signal, text=signal_text)
    eligible = _semantic_cue_is_eligible(signal_text)
    source_roles = tuple(authority_info.get("source_roles") or _source_role_groups(binding, row_refs))
    source_role = source_roles[0] if source_roles else "unbound"
    # An explicit placeholder marker is an immutable context barrier.  It is
    # intentionally separate from the typed-media check because a genuine
    # non-placeholder caption may still be a bounded primary unit.
    hard_placeholder = _record_is_hard_placeholder(row) or _record_is_hard_placeholder(authority)
    provider_media_or_authority = _projection_record_is_provider_media(row) or _projection_record_is_provider_media(authority)
    authority_type = _normalise_semantic_label(
        authority.get("message_type", authority.get("type", "")) if isinstance(authority, Mapping) else ""
    )
    row_type = _normalise_semantic_label(row.get("message_type", row.get("type", "")))
    barrier_type = authority_type or row_type
    barrier_labels = authority_labels if authority_labels else labels
    authority_barrier = bool(barrier_labels & _PROVIDER_AUTHORITY_BARRIER_ROLES) or barrier_type in _PROVIDER_AUTHORITY_BARRIER_TYPES
    authority_barrier = authority_barrier or any(
        source.get(key) is True or source.get(key) == 1
        for source in (row, authority)
        if isinstance(source, Mapping)
        for key in ("authority_only", "is_authority", "is_placeholder", "placeholder", "media_placeholder", "event_placeholder")
    )
    authority_barrier = authority_barrier or hard_placeholder
    # A barrier from an explicit authority row can be crossed only by a
    # direct human caption/text and a positive role in that same authority
    # metadata.  For body-only synthetic/linear rows, a source-primary role is
    # the compatibility equivalent of the positive role; merged candidate
    # fields never satisfy this exception.
    valid_caption_role = bool(authority_labels & _PROVIDER_POSITIVE_ROLES) if authority_labels else bool(labels & _PROVIDER_POSITIVE_ROLES) or "primary" in source_roles
    caption_authority_role_valid = True
    if barrier_type in {"system", "event", "event_message"}:
        caption_authority_role_valid = _record_has_positive_authority_role(row) or _record_has_positive_authority_role(authority)
    caption_exception = bool(
        direct_human_cue
        and not hard_placeholder
        and caption_authority_role_valid
        and not _has_merged_candidate_cue(row)
        and (
            valid_caption_role
            # Older synthetic/linear rows may carry the source-primary role
            # but no separate authority role.  Their direct lexical cue is
            # the bounded message evidence; an explicit authority row never
            # enters this compatibility branch.
            or (
                not authority_info.get("explicit")
                and "primary" in source_roles
                and eligible
            )
        )
    )
    media_caption_valid = bool(direct_human_cue) and _media_caption_source_role_is_valid(
        row,
        source_roles=source_roles,
    )
    semantic_positive = bool(labels & _PROVIDER_SUBSTANTIVE_ROLES)
    explicit_primary = "primary" in labels
    explicit_context = bool(labels & _PROVIDER_CONTEXT_ROLES)
    fragment_type = _normalise_semantic_label(row.get("fragment_type", row.get("kind", "")))
    fragment_positive = fragment_type in _PROVIDER_SUBSTANTIVE_FRAGMENT_TYPES
    hard_marker = any(
        source.get(key) is True or source.get(key) == 1
        for source in nested
        for key in (
            "authority",
            "authority_only",
            "is_authority",
            "context_only",
            "is_context_only",
            "is_adjacent",
            "candidate",
            "is_candidate",
        )
    )
    strong_signal = bool(semantic_positive or fragment_positive or signal or media_caption_valid)
    projected = "context"
    promoted_adjacent = False
    conflict_reasons: List[str] = []
    telemetry_codes: List[str] = []

    if len(source_roles) > 1:
        conflict_reasons.append("multiple_source_roles")
    # Authority identity and a concrete span are mandatory before any
    # substantive cue can become a provider primary.  This closes the
    # residual path where a merged candidate cue was previously promoted.
    if not authority_info.get("bound"):
        reason = "unbound_substantive_candidate_not_promoted" if strong_signal or _has_merged_candidate_cue(row) else "unbound_context"
        if strong_signal or _has_merged_candidate_cue(row):
            telemetry_codes.append("unbound_substantive_candidate_not_promoted")
        if explicit_primary or strong_signal:
            conflict_reasons.append("authoritative_message_binding_missing")
    elif not authority_info.get("evidence_span") and ("primary" not in source_roles or _has_merged_candidate_cue(row)):
        reason = "unbound_substantive_candidate_not_promoted"
        telemetry_codes.append("unbound_substantive_candidate_not_promoted")
        if strong_signal:
            conflict_reasons.append("evidence_span_missing")
    elif authority_barrier and not caption_exception:
        reason = "media_or_empty_authority_context"
        if direct_human_cue:
            conflict_reasons.append("authority_caption_role_invalid")
    elif not eligible:
        reason = "context_only_text" if text else "empty_or_no_cue"
    elif "candidate" in source_roles:
        reason = "candidate_context"
        if strong_signal or explicit_primary:
            conflict_reasons.append("candidate_semantic_conflict")
    elif "authority" in source_roles:
        # Authority records remain context unless their immutable metadata
        # itself carries a positive role and semantic signal.  In particular,
        # a context+authority overlap is not enough to promote a direct
        # caption: the source projection must also bind that row as primary.
        # This is deliberately conservative when a reviewed human baseline
        # disagrees with a newly surfaced legal caption; the caption remains
        # recoverable context instead of silently changing the baseline.
        if (
            "primary" in source_roles
            and
            (
                bool(authority_labels & _PROVIDER_SUBSTANTIVE_ROLES)
                or fragment_positive
                or (
                    caption_exception
                    and "primary" in source_roles
                    and valid_caption_role
                )
            )
            and not hard_marker
        ):
            projected = "primary"
            reason = "authority_substantive"
            conflict_reasons.append("authority_substantive_override")
        else:
            reason = "authority_context"
    elif "primary" in source_roles:
        if hard_marker or (explicit_context and not strong_signal and authority_info.get("explicit")):
            reason = "source_primary_role_context"
            if eligible and explicit_context:
                conflict_reasons.append("source_primary_context_conflict")
        else:
            projected = "primary"
            reason = "source_primary_semantic" if strong_signal else "source_primary"
            if explicit_context:
                conflict_reasons.append("source_primary_context_label")
    elif "context" in source_roles:
        if strong_signal and not hard_marker:
            projected = "primary"
            promoted_adjacent = True
            reason = "substantive_adjacent_promoted"
            if explicit_primary:
                conflict_reasons.append("source_context_primary_label")
        else:
            reason = "source_context"
    else:
        reason = "unbound_context"

    if not eligible or (authority_barrier and not caption_exception):
        projected = "context"
    if projected == "primary" and (not authority_info.get("bound") or (not authority_info.get("evidence_span") and _has_merged_candidate_cue(row))):
        projected = "context"
        if "unbound_substantive_candidate_not_promoted" not in telemetry_codes:
            telemetry_codes.append("unbound_substantive_candidate_not_promoted")
        if reason not in {"unbound_substantive_candidate_not_promoted", "unbound_context"}:
            conflict_reasons.append("authoritative_message_binding_missing")
    return {
        "projected_role": projected,
        "source_role": source_role,
        "source_roles": tuple(source_roles),
        "eligible": bool(eligible),
        "promoted_adjacent": bool(promoted_adjacent and projected == "primary"),
        "role_conflict": bool(conflict_reasons),
        "conflict_reasons": tuple(sorted(set(conflict_reasons))),
        "reason": reason,
        "telemetry_codes": tuple(sorted(set(telemetry_codes))),
        "authoritative_message_bound": bool(authority_info.get("bound")),
        "authoritative_metadata_explicit": bool(authority_info.get("explicit")),
        "evidence_span_bound": bool(authority_info.get("evidence_span")),
        "direct_human_caption": bool(direct_human_cue),
        # These are scalar barrier markers used only for body-free audit
        # counts.  A valid direct caption may cross a typed media/system
        # barrier; the corresponding mispromotion counter therefore checks
        # the caption marker before incrementing.
        "provider_media_barrier": bool(provider_media_or_authority),
        "hard_placeholder_barrier": bool(hard_placeholder),
        "authority_barrier": bool(authority_barrier),
        "reaction_barrier": bool(labels.intersection({"reaction"})),
        "empty_authority_barrier": bool(labels.intersection({"empty", "empty_authority", "authority_only"})),
        "barrier_type": str(barrier_type),
    }


def _role(
    row: Mapping[str, Any],
    handle: str,
    page: Mapping[str, Any],
    root: Mapping[str, Any],
    *,
    text: str = "",
) -> str:
    return str(_projected_message_role(row, handle, page, root, text=text)["projected_role"])


def _role_projection_telemetry(
    page: Mapping[str, Any],
    request: Mapping[str, Any],
    role_infos: Optional[Sequence[Mapping[str, Any]]] = None,
    *,
    root: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return body-free source-vs-provider role projection telemetry."""

    source_root = root if isinstance(root, Mapping) else {}
    binding = _source_primary_binding(page, source_root)
    message_rows = [
        row for row in request.get("h", ())
        if isinstance(row, Mapping) and row.get("k") == "m"
    ]
    projected_primary_count = sum(row.get("r") == "p" for row in message_rows)
    projected_context_count = sum(row.get("r") == "c" for row in message_rows)
    source_counts: Dict[str, int] = {
        "primary": 0,
        "context": 0,
        "authority": 0,
        "candidate": 0,
        "unbound": 0,
    }
    promotion_count = 0
    conflict_count = 0
    conflict_reasons: set[str] = set()
    telemetry_codes: set[str] = set()
    mispromotion_counts: Dict[str, int] = {
        "media": 0,
        "empty_authority": 0,
        "placeholder": 0,
        "reaction": 0,
        "system_or_event": 0,
        "unbound": 0,
    }
    infos = list(role_infos or ())
    for index, row in enumerate(message_rows):
        info = infos[index] if index < len(infos) and isinstance(infos[index], Mapping) else None
        if info is None:
            refs = {str(row.get("h", ""))}
            groups = _source_role_groups(binding, refs)
            source_role = groups[0] if groups else "unbound"
            source_roles = groups
            projected = str(row.get("r", "c"))
            promoted = False
            conflict = False
            reasons: Sequence[str] = ()
        else:
            source_role = str(info.get("source_role") or "unbound")
            source_roles = tuple(str(item) for item in info.get("source_roles", ()) if item)
            projected = str(info.get("projected_role") or row.get("r", "c"))
            promoted = bool(info.get("promoted_adjacent"))
            conflict = bool(info.get("role_conflict"))
            reasons = tuple(str(item) for item in info.get("conflict_reasons", ()) if item)
            telemetry_codes.update(str(item) for item in info.get("telemetry_codes", ()) if item)
            if projected == "primary":
                # A typed media/system/event row with an explicitly bound,
                # direct human caption is the narrow accepted exception.  It
                # is not a mispromotion; all other barrier crossings are
                # counted here without retaining the row or its cue.
                direct_caption = bool(info.get("direct_human_caption"))
                if bool(info.get("provider_media_barrier")) and not direct_caption:
                    mispromotion_counts["media"] += 1
                if bool(info.get("empty_authority_barrier")) and not direct_caption:
                    mispromotion_counts["empty_authority"] += 1
                if bool(info.get("hard_placeholder_barrier")):
                    mispromotion_counts["placeholder"] += 1
                if bool(info.get("reaction_barrier")):
                    mispromotion_counts["reaction"] += 1
                if str(info.get("barrier_type", "")) in {"system", "event", "event_message"} and not direct_caption:
                    mispromotion_counts["system_or_event"] += 1
                if not bool(info.get("authoritative_message_bound")):
                    mispromotion_counts["unbound"] += 1
        if source_role not in source_counts:
            source_role = "unbound"
        source_counts[source_role] += 1
        for role_name in source_roles:
            if role_name in source_counts and role_name != source_role:
                # Overlapping source memberships are represented separately
                # in the role map and are therefore visible as a conflict,
                # without retaining the row or its cue.
                source_counts[role_name] += 1
        promotion_count += int(promoted and projected == "primary")
        if conflict:
            conflict_count += 1
            conflict_reasons.update(reasons)
        if info is None:
            continue
        telemetry_codes.update(str(item) for item in info.get("telemetry_codes", ()) if item)
    source_context_count = source_counts["context"] + source_counts["authority"] + source_counts["candidate"]
    try:
        topic_limit = len(_semantic_primary_aliases_from_packet(request)) if message_rows else 0
    except Exception:
        topic_limit = int(projected_primary_count)
    cause = "projected_primary_count" if projected_primary_count else "no_projected_primary"
    telemetry_code_counts = {
        code: sum(
            code in tuple(info.get("telemetry_codes", ()))
            for info in infos
            if isinstance(info, Mapping)
        )
        for code in sorted(telemetry_codes)
    }
    return {
        "source_role_counts": dict(source_counts),
        "source_primary_count": int(source_counts["primary"]),
        "source_context_count": int(source_context_count),
        "source_adjacent_count": int(source_counts["context"]),
        "source_authority_count": int(source_counts["authority"]),
        "source_candidate_count": int(source_counts["candidate"]),
        "source_nonprimary_count": int(source_context_count),
        "projected_primary_count": int(projected_primary_count),
        "projected_context_count": int(projected_context_count),
        "source_role_primary_count": int(source_counts["primary"]),
        "source_role_context_count": int(source_context_count),
        "source_role_vs_projected": {
            "source_primary": int(source_counts["primary"]),
            "source_context": int(source_context_count),
            "projected_primary": int(projected_primary_count),
            "projected_context": int(projected_context_count),
        },
        "substantive_adjacent_promoted_count": int(promotion_count),
        "role_conflict_count": int(conflict_count),
        "role_conflict": bool(conflict_count),
        "role_conflict_reasons": sorted(conflict_reasons),
        "topic_limit": int(topic_limit),
        "topic_limit_cause": cause,
        "topic_limit_cause_code": cause,
        "telemetry_codes": sorted(telemetry_codes),
        "telemetry_code_counts": telemetry_code_counts,
        "unbound_substantive_candidate_not_promoted_count": int(
            telemetry_code_counts.get("unbound_substantive_candidate_not_promoted", 0)
        ),
        "media_mispromotion_count": int(mispromotion_counts["media"]),
        "empty_authority_mispromotion_count": int(mispromotion_counts["empty_authority"]),
        "placeholder_mispromotion_count": int(mispromotion_counts["placeholder"]),
        "reaction_mispromotion_count": int(mispromotion_counts["reaction"]),
        "system_or_event_mispromotion_count": int(mispromotion_counts["system_or_event"]),
        "unbound_primary_promotion_count": int(mispromotion_counts["unbound"]),
        "barrier_mispromotion_counts": dict(mispromotion_counts),
        "barrier_mispromotion_zero": not any(mispromotion_counts.values()),
        "body_free": True,
    }


def _normalise_semantic_label(value: Any) -> str:
    return str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")


def _truthy_marker(source: Mapping[str, Any], *keys: str) -> bool:
    return any(source.get(key) is True or source.get(key) == 1 for key in keys)


def _typed_slots_are_canonical(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    required = ("person", "object", "state")
    if not all(key in value for key in required):
        return False
    unresolved = {"", "unknown", "uncertain", "unresolved", "missing", "none", "null"}
    resolved = {"canonical", "explicit", "resolved", "known", "typed", "typed_resolved"}
    labels = {_normalise_semantic_label(value[key]) for key in required}
    return bool(labels) and labels <= resolved and not labels.intersection(unresolved)


def _canonical_typed_resolution_available(sources: Sequence[Mapping[str, Any]]) -> bool:
    """Recognise only explicit canonical typed resolution markers.

    Surface cues, strong candidate relations and non-empty candidate fields do
    not establish a typed resolution.  This deliberately narrow gate prevents
    a candidate from being laundered into a fact merely because it has a
    person/object/state-shaped label.
    """

    canonical_statuses = {"canonical", "complete", "resolved", "typed_resolved", "explicit"}
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        if _truthy_marker(
            source,
            "canonical_typed_resolution",
            "canonical_typed_resolved",
            "typed_resolution_canonical",
            "canonical_reference_resolution",
        ):
            return True
        for key in (
            "canonical_typed_resolution",
            "canonical_reference_resolution",
            "typed_resolution",
            "typed_resolution_status",
        ):
            value = source.get(key)
            if isinstance(value, Mapping) and _typed_slots_are_canonical(value):
                return True
        status = _normalise_semantic_label(source.get("canonical_metadata_status"))
        slots = source.get("typed_evidence_slots")
        if status in canonical_statuses and _typed_slots_are_canonical(slots):
            return True
        if status in canonical_statuses:
            fields = source.get("typed_evidence_fields")
            if isinstance(fields, (list, tuple, set, frozenset)) and {
                _normalise_semantic_label(item) for item in fields
            } >= {"person", "object", "state"}:
                return True
    return False


def _page_semantic_sources(
    page: Mapping[str, Any],
    root: Mapping[str, Any],
    store: Optional[Mapping[str, Any]],
) -> List[Mapping[str, Any]]:
    sources: List[Mapping[str, Any]] = [page, root]
    materialized = page.get("_materialized_meta")
    if isinstance(materialized, Mapping):
        sources.append(materialized)
    handles = list(
        _safe_handle_values(page.get("candidate_handles", page.get("candidate_link_refs", ())))
    )
    handles.extend(_safe_handle_values(root.get("candidate_handles", ())))
    for handle in dict.fromkeys(str(item) for item in handles if item):
        row = _find_store_row(store, handle, "c")
        if row:
            sources.append(row)
    return sources


def _page_has_unresolved_pronoun_candidate(
    page: Mapping[str, Any],
    root: Mapping[str, Any],
    store: Optional[Mapping[str, Any]],
) -> bool:
    labels: set[str] = set()
    for source in (page, root):
        for key in (
            "categories",
            "category",
            "category_flags",
            "selection_categories",
            "selection_strata",
            "strata",
            "cue_families",
            "candidate_reasons",
            "reason_codes",
        ):
            value = source.get(key)
            if isinstance(value, Mapping):
                values = [name for name, enabled in value.items() if enabled is True or enabled == 1]
            elif isinstance(value, (list, tuple, set, frozenset)):
                values = list(value)
            else:
                values = [value]
            labels.update(_normalise_semantic_label(item) for item in values if item not in (None, ""))
    if "pronoun_person_object_state" not in labels:
        return False
    sources = _page_semantic_sources(page, root, store)
    candidate_only = any(
        _truthy_marker(source, "candidate_only", "_candidate_only")
        for source in sources
    )
    pending = any(
        _truthy_marker(source, "semantic_decision_pending", "_semantic_decision_pending")
        for source in sources
    )
    not_canonical = any(
        _truthy_marker(source, "not_canonical", "_not_canonical")
        for source in sources
    )
    # Candidate-only selection metadata is expected to carry all three flags;
    # accepting candidate_only alone keeps synthetic tests useful while the
    # canonical-resolution check below remains fail-closed.
    if not candidate_only or (not pending and not not_canonical):
        return False
    return not _canonical_typed_resolution_available(sources)


_CONTEXT_HINT_METADATA_KEYS = frozenset(
    {
        "subject_id", "subject_ref_id", "subject_ref", "subject", "subject_entity_id",
        "object_id", "object_ref_id", "object_ref", "object", "target_id", "target_ref_id",
        "action", "action_id", "action_ref", "actions", "actions_candidate", "action_candidate", "intent",
        "state", "state_id", "state_candidate", "state_ref", "status",
        "reply_to_message_id", "reply_to", "in_reply_to", "reply_target", "reply_of", "answer_to", "question_id",
        "left_message_id", "right_message_id", "source_message_ids", "message_ids", "member_message_ids",
        "primary_message_ids", "context_message_ids", "related_message_ids", "continuation_refs", "continuation_ref",
        "relation", "relation_label", "relation_subtype", "relation_type", "semantic_relation", "continuation",
        "qa_relation", "question_answer", "same_topic", "same_subject", "same_object", "same_action",
        "state_change", "object_inheritance", "topic_shift", "topic_change", "new_topic", "is_opener", "is_new_topic",
        "fragment_type", "kind", "unit_kind", "semantic_kind", "semantic_role", "provider_role",
        "primary_context_role", "role", "layer", "message_role", "cue_family", "cue_type",
        "opening", "is_opening", "conversation_opener", "opener", "greeting",
        # The compact protocol follows only these explicitly named wrappers
        # and then reads its narrow scalar metadata fields.
        "identity_row", "authoritative", "authority", "source_row", "mapping_row",
        "message_metadata", "metadata",
    }
)


def _context_hint_metadata(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Copy only narrow identity/relation metadata into a preflight row."""

    if not isinstance(row, Mapping):
        return {}
    return {str(key): row[key] for key in _CONTEXT_HINT_METADATA_KEYS if key in row}


def _context_relevance_families(
    row: Mapping[str, Any],
    page: Mapping[str, Any],
    handle: str,
) -> Tuple[str, ...]:
    """Return fixed relevance families from shallow, body-free metadata."""

    if not isinstance(row, Mapping):
        row = {}
    labels: set[str] = set()
    for key in (
        "relation",
        "relation_type",
        "relation_subtype",
        "fragment_type",
        "kind",
        "semantic_role",
        "role",
        "cue_family",
        "cue_type",
    ):
        value = row.get(key)
        if isinstance(value, (list, tuple, set, frozenset)):
            labels.update(_normalise_semantic_label(item) for item in value)
        elif value not in (None, ""):
            labels.add(_normalise_semantic_label(value))
    families: List[str] = []
    reply_keys = (
        "reply_to_message_id",
        "reply_to",
        "in_reply_to",
        "reply_target",
        "reply_of",
        "answer_to",
        "question_id",
        "reply_message_id",
    )
    quote_keys = (
        "quote_message_id",
        "quoted_message_id",
        "quote_to",
        "quoted_from",
        "quote_ref",
        "quoted_ref",
    )
    if any(row.get(key) not in (None, "", (), [], {}) for key in reply_keys):
        families.append("reply")
    if any(row.get(key) not in (None, "", (), [], {}) for key in quote_keys):
        families.append("quote")
    qa_labels = {
        "qa",
        "question",
        "answer",
        "question_answer",
        "question_follow_up",
        "question_followup",
        "reply",
        "continuation",
        "continues",
        "elaborates",
    }
    if labels.intersection(qa_labels):
        families.append("qa")
    boundary_keys = (
        "topic_shift",
        "topic_change",
        "new_topic",
        "topic_boundary",
        "is_topic_boundary",
        "is_new_topic",
    )
    if any(row.get(key) is True or row.get(key) == 1 for key in boundary_keys) or labels.intersection(
        {"topic_shift", "topic_change", "new_topic", "topic_boundary"}
    ):
        families.append("topic_boundary")
    opening_keys = (
        "opening",
        "is_opening",
        "conversation_opener",
        "is_opener",
        "opener",
        "greeting",
    )
    if any(row.get(key) is True or row.get(key) == 1 for key in opening_keys) or labels.intersection(
        {"opening", "opener", "conversation_opener", "greeting"}
    ):
        families.append("opening")
    adjacent_handles: set[str] = set()
    for key in (
        "adjacent_message_handles",
        "adjacent_message_ids",
        "adjacent_handles",
        "context_message_handles",
    ):
        adjacent_handles.update(_safe_handle_values(page.get(key)))
    if handle in adjacent_handles or _normalise_semantic_label(row.get("role")) in {
        "context",
        "context_only",
        "adjacent",
    }:
        families.append("adjacent")
    if not families:
        families.append("fallback")
    # Stable de-duplication is important when an upstream row repeats a marker
    # through two wrappers; it must not receive two selection slots.
    return tuple(dict.fromkeys(families))


def _context_relevance_priority(families: Sequence[str]) -> int:
    positions = {
        name: index for index, name in enumerate(TOPIC_GUIDED_CONTEXT_SELECTION_PRIORITY)
    }
    return min((positions.get(str(name), len(positions)) for name in families), default=len(positions))


def _context_reason_counts(
    entries: Sequence[Mapping[str, Any]],
) -> Dict[str, int]:
    counts = {name: 0 for name in TOPIC_GUIDED_CONTEXT_SELECTION_PRIORITY}
    for entry in entries:
        families = entry.get("families", ())
        if not isinstance(families, (list, tuple, set, frozenset)):
            families = ("fallback",)
        family = min(
            (str(item) for item in families if str(item) in counts),
            key=lambda item: TOPIC_GUIDED_CONTEXT_SELECTION_PRIORITY.index(item),
            default="fallback",
        )
        counts[family] = counts.get(family, 0) + 1
    return {key: value for key, value in counts.items() if value}


def _body_free_context_selection(
    *,
    all_context: Sequence[Mapping[str, Any]],
    kept_context: Sequence[Mapping[str, Any]],
    request: Optional[Mapping[str, Any]] = None,
    deferred_reason: str = "over_budget",
) -> Dict[str, Any]:
    total = len(all_context)
    kept = len(kept_context)
    deferred_entries = [entry for entry in all_context if entry not in kept_context]
    selection: Dict[str, Any] = {
        "version": TOPIC_GUIDED_CONTEXT_SELECTION_VERSION,
        "telemetry_version": TOPIC_GUIDED_CONTEXT_TELEMETRY_VERSION,
        "calibration_version": TOPIC_GUIDED_TOKEN_CALIBRATION_VERSION,
        "context_total": int(total),
        "context_kept": int(kept),
        "context_deferred": int(max(0, total - kept)),
        "kept": int(kept),
        "deferred": int(max(0, total - kept)),
        "context_coverage": (float(kept) / float(total)) if total else 1.0,
        "context_coverage_count": int(kept),
        "context_coverage_total": int(total),
        "kept_reason_counts": _context_reason_counts(kept_context),
        "deferred_reason_counts": _context_reason_counts(deferred_entries),
        "reasons": {
            "kept": _context_reason_counts(kept_context),
            "deferred": _context_reason_counts(deferred_entries),
            "deferred_policy": str(deferred_reason),
        },
        "primary_never_deferred": True,
        "context_role": "c",
        "recovery_available": True,
        "body_free": True,
    }
    if isinstance(request, Mapping):
        try:
            stats = measure_wire_size(request)
            selection.update(
                {
                    "http_token_proxy": int(stats.http_token_proxy),
                    "proxy": int(stats.http_token_proxy),
                    "calibrated_input_token_proxy": int(stats.calibrated_input_proxy),
                    "calibrated_proxy": int(stats.calibrated_input_proxy),
                    "calibrated_within_limit": bool(
                        stats.calibrated_input_proxy <= CALIBRATED_INPUT_TOKEN_PROXY_LIMIT
                    ),
                }
            )
        except Exception:
            pass
    return selection


def context_recovery_snapshot(page: Mapping[str, Any]) -> Dict[str, Any]:
    """Expose an opaque in-memory recovery/open snapshot for one page.

    The full context handles remain available to an offline caller, while the
    public artifact writer intentionally records only counts and a snapshot
    hash.  No message body or raw provider payload is copied here.
    """

    raw = page.get("_context_recovery_snapshot") if isinstance(page, Mapping) else None
    if not isinstance(raw, Mapping):
        return {
            "version": TOPIC_GUIDED_CONTEXT_SELECTION_VERSION,
            "complete_context_handles": [],
            "kept_context_handles": [],
            "deferred_context_handles": [],
            "source_ref_count": 0,
            "body_free": True,
        }
    result = {
        "version": str(raw.get("version", TOPIC_GUIDED_CONTEXT_SELECTION_VERSION)),
        "complete_context_handles": [str(item) for item in raw.get("complete_context_handles", ())],
        "kept_context_handles": [str(item) for item in raw.get("kept_context_handles", ())],
        "deferred_context_handles": [str(item) for item in raw.get("deferred_context_handles", ())],
        "source_ref_count": int(raw.get("source_ref_count", 0) or 0),
        "body_free": True,
    }
    result["snapshot_sha256"] = stable_hash(
        {
            "version": result["version"],
            "complete_context_handles": result["complete_context_handles"],
            "kept_context_handles": result["kept_context_handles"],
            "deferred_context_handles": result["deferred_context_handles"],
        }
    )
    return result


def _select_provider_context_view(
    *,
    scope: Mapping[str, Any],
    primary_entries: Sequence[Mapping[str, Any]],
    context_entries: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    uncertainty_ceiling: Optional[str],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Select the most relevant context rows until the calibrated gate passes."""

    # Rank only context rows.  Primary entries are never in this loop and are
    # always retained in the provider view.
    ranked = sorted(
        context_entries,
        key=lambda entry: (
            _context_relevance_priority(entry.get("families", ("fallback",))),
            int(entry.get("source_index", 0) or 0),
            str(entry.get("handle", "")),
        ),
    )
    # One identity/family gets one provider slot.  The recovery snapshot still
    # retains every source handle, including deduplicated/deferred rows.
    unique_ranked: List[Mapping[str, Any]] = []
    seen_family_identity: set[Tuple[str, str]] = set()
    for entry in ranked:
        identity = str(entry.get("identity") or entry.get("handle") or "")
        families = tuple(str(item) for item in entry.get("families", ()) if item)
        family = min(families, key=lambda item: TOPIC_GUIDED_CONTEXT_SELECTION_PRIORITY.index(item) if item in TOPIC_GUIDED_CONTEXT_SELECTION_PRIORITY else 999)
        key = (identity, family)
        if key in seen_family_identity:
            continue
        seen_family_identity.add(key)
        unique_ranked.append(entry)

    # Try the full context set first, then defer the least relevant tail.  A
    # new wire packet is built for every trial so g remains deterministic for
    # the exact provider view; no local topic assignment is made.
    last_error: Optional[BaseException] = None
    for count in range(len(unique_ranked), -1, -1):
        kept_set = {id(entry) for entry in unique_ranked[:count]}
        selected_context = [
            entry for entry in context_entries if id(entry) in kept_set
        ]
        ordered_entries = sorted(
            list(primary_entries) + selected_context,
            key=lambda entry: int(entry.get("source_index", 0) or 0),
        )
        messages = [entry["provider_message"] for entry in ordered_entries]
        try:
            request = build_compact_stage_a_request(
                scope,
                messages,
                candidates,
                uncertainty_ceiling=uncertainty_ceiling,
            )
            stats = measure_wire_size(request)
            if stats.calibrated_input_proxy > CALIBRATED_INPUT_TOKEN_PROXY_LIMIT:
                raise CompactStageADevelopmentError("input_token_limit_exceeded")
        except (CompactStageAProtocolV3Error, CompactStageADevelopmentError) as exc:
            last_error = exc
            continue
        selection = _body_free_context_selection(
            all_context=context_entries,
            kept_context=selected_context,
            request=request,
        )
        selection["deduplicated_context_count"] = int(max(0, len(context_entries) - len(unique_ranked)))
        selection["candidate_context_count"] = int(len(context_entries))
        return request, selection
    if isinstance(last_error, CompactStageADevelopmentError):
        raise last_error
    if isinstance(last_error, CompactStageAProtocolV3Error):
        raise last_error
    raise CompactStageADevelopmentError("request_input_token_proxy_exceeded")


def _build_request_selected(page: Mapping[str, Any], store: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Build a v3 request after deterministic context-view selection."""

    scope = _scope(page.get("scope"))
    root = _store_root(store, str(page.get("_store_root_id", page.get("root_id", ""))))
    source_binding = _source_primary_binding(page, root)
    if isinstance(page, MutableMapping):
        page["_source_primary_sha256"] = str(source_binding["sha256"])

    # Build the complete message set first.  Context rows may be deferred, but
    # primary rows are never truncated by this union or by the token gate.
    message_handles: List[str] = []
    for values in (
        page.get("primary_message_handles", ()),
        page.get("_provider_message_handles", ()),
        page.get("adjacent_message_handles", ()),
        page.get("message_handles", ()),
    ):
        for value in _safe_handle_values(values):
            if value and value not in message_handles:
                message_handles.append(str(value))
    if not message_handles:
        for row in _rows(page.get("message_rows")) + _rows(page.get("messages")):
            value = _id_from_row(row, "message_handle", "message_id", "handle", "id")
            if value and value not in message_handles:
                message_handles.append(value)
    candidate_handles: List[str] = []
    for value in _safe_handle_values(page.get("candidate_handles", page.get("candidate_link_refs", ()))):
        if value and value not in candidate_handles:
            candidate_handles.append(str(value))

    entries: List[Dict[str, Any]] = []
    seen_message_ids: set[str] = set()
    fallback_rows = _rows(page.get("message_rows")) + _rows(page.get("messages"))
    for source_index, handle in enumerate(message_handles):
        row = _find_store_row(store, handle, "m")
        text = _extract_text(row, store)
        if not row:
            for candidate in fallback_rows:
                if _id_from_row(candidate, "message_handle", "message_id", "handle", "id") == handle:
                    row = candidate
                    text = _extract_text(row, store)
                    break
        identity = _message_identity_for_dedup(row)
        if identity and identity.casefold() != "unknown" and identity in seen_message_ids:
            continue
        if identity and identity.casefold() != "unknown":
            seen_message_ids.add(identity)
        role_info = _projected_message_role(row, handle, page, root, text=text, store=store)
        projected_role = str(role_info.get("projected_role", "context"))
        provider_type = row.get("message_type", row.get("type", "text")) if isinstance(row, Mapping) else "text"
        # Force every projected context row through the protocol's typed
        # context path.  Leaving a context+authority text row as ``text``
        # lets the generic protocol builder re-promote its direct cue while
        # constructing the wire packet, violating the strict r=c barrier.
        if projected_role != "primary":
            provider_type = "system"
        # Metadata is copied only into this short-lived preflight record so
        # the existing g extractor can preserve subject/reply/QA boundaries.
        # The compact request builder emits only the bounded cue and role;
        # no arbitrary field below reaches the provider wire.
        provider_message: Dict[str, Any] = _context_hint_metadata(row)
        provider_message.update({
            "handle": handle,
            "role": projected_role,
            "text": text,
            "message_type": provider_type,
            "semantic_role": "substantive" if projected_role == "primary" else "context",
        })
        if (
            isinstance(row, Mapping)
            and (
                row.get("_context_direct_caption") is True
                or any(row.get(key) not in (None, "") for key in ("caption", "text_redacted", "message_text"))
            )
            and text
            and provider_type in {"system", "event", "event_message"}
        ):
            provider_message["caption"] = text
            if role_info.get("direct_human_caption") and projected_role == "primary":
                provider_message["roles"] = ["primary", "authority", "substantive"]
        entry = {
            "handle": handle,
            "identity": identity if identity and identity.casefold() != "unknown" else handle,
            "source_index": source_index,
            "role_info": role_info,
            "projected_role": projected_role,
            "provider_message": provider_message,
            "families": _context_relevance_families(row, page, handle),
        }
        entries.append(entry)

    primary_entries = [entry for entry in entries if entry.get("projected_role") == "primary"]
    context_entries = [entry for entry in entries if entry.get("projected_role") != "primary"]
    if len(primary_entries) > 14:
        raise CompactStageADevelopmentError("request_message_limit")
    candidates: List[Dict[str, Any]] = []
    for handle in candidate_handles:
        candidate_row = _find_store_row(store, handle, "c")
        candidate = dict(candidate_row) if isinstance(candidate_row, Mapping) else {}
        candidate["handle"] = handle
        candidates.append(candidate)
    if isinstance(page, MutableMapping):
        page["_context_recovery_snapshot"] = {
            "version": TOPIC_GUIDED_CONTEXT_SELECTION_VERSION,
            "complete_context_handles": [str(entry["handle"]) for entry in context_entries],
            "kept_context_handles": [],
            "deferred_context_handles": [str(entry["handle"]) for entry in context_entries],
            "source_ref_count": len(message_handles) + len(candidate_handles),
        }
    ceiling = "uncertain" if _page_has_unresolved_pronoun_candidate(page, root, store) else None
    try:
        request, selection = _select_provider_context_view(
            scope=scope,
            primary_entries=primary_entries,
            context_entries=context_entries,
            candidates=candidates,
            uncertainty_ceiling=ceiling,
        )
        selected_handles = {
            str(row.get("h", ""))
            for row in request.get("h", ())
            if isinstance(row, Mapping) and row.get("k") == "m" and row.get("r") == "c"
        }
        candidate_components = build_candidate_connected_components(request)
        component_telemetry = {
            "version": str(candidate_components.get("version", "")),
            "component_count": len(candidate_components.get("components", ())),
            "split_evidence_count": int(candidate_components.get("split_evidence_count", 0) or 0),
            "split_evidence_families": [str(item) for item in candidate_components.get("split_evidence_families", ())],
            "merge_prior": str(candidate_components.get("merge_prior", "")),
            "candidate_only": True,
            "model_decision_required": True,
            "final_topic_assignment": False,
            "under_uncertainty": "prefer_merge",
            "no_one_message_per_topic": True,
            "body_free": True,
        }
        selection["candidate_components"] = {
            "component_count": component_telemetry["component_count"],
            "split_evidence_count": component_telemetry["split_evidence_count"],
            "merge_prior": component_telemetry["merge_prior"],
            "candidate_only": True,
            "model_decision_required": True,
            "final_topic_assignment": False,
            "under_uncertainty": "prefer_merge",
            "no_one_message_per_topic": True,
        }
        if isinstance(page, MutableMapping):
            page["_context_selection_telemetry"] = dict(selection)
            page["_candidate_component_telemetry"] = component_telemetry
            page["_context_recovery_snapshot"] = {
                "version": TOPIC_GUIDED_CONTEXT_SELECTION_VERSION,
                "complete_context_handles": [str(entry["handle"]) for entry in context_entries],
                "kept_context_handles": [str(entry["handle"]) for entry in context_entries if str(entry["handle"]) in selected_handles],
                "deferred_context_handles": [str(entry["handle"]) for entry in context_entries if str(entry["handle"]) not in selected_handles],
                "source_ref_count": len(message_handles) + len(candidate_handles),
            }
            selected_entries = [
                entry for entry in entries if entry.get("projected_role") == "primary" or str(entry.get("handle")) in selected_handles
            ]
            selected_infos = [entry.get("role_info", {}) for entry in selected_entries]
            page["_source_primary_eligible"] = bool(
                any(row.get("k") == "m" and row.get("r") == "p" for row in request.get("h", ()) if isinstance(row, Mapping))
                and source_binding["provided"]
                and all(bool(info.get("authoritative_message_bound")) for info in selected_infos if isinstance(info, Mapping) and info.get("projected_role") == "primary")
            )
            page["_role_projection_telemetry"] = _role_projection_telemetry(
                page, request, selected_infos, root=root
            )
        return request
    except Exception as exc:
        if isinstance(page, MutableMapping):
            page["_source_primary_eligible"] = False
        code = str(getattr(exc, "code", "page_request_invalid"))
        raise CompactStageADevelopmentError(code) from exc


def _store_root(store: Optional[Mapping[str, Any]], root_id: str) -> Mapping[str, Any]:
    for row in _rows(store.get("roots") if isinstance(store, Mapping) else None):
        if str(row.get("root_id", row.get("packet_id", ""))) == root_id:
            return row
    roots = store.get("root_table") if isinstance(store, Mapping) else None
    if isinstance(roots, Mapping) and isinstance(roots.get(root_id), Mapping):
        return roots[root_id]
    return {}


def _build_request(page: Mapping[str, Any], store: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    return _build_request_selected(page, store)

    scope = _scope(page.get("scope"))
    root = _store_root(store, str(page.get("_store_root_id", page.get("root_id", ""))))
    source_binding = _source_primary_binding(page, root)
    # Keep only scalar provenance on the in-memory page so PageRun can expose
    # it in body-free telemetry.  The wire request remains the exact v3
    # schema; adding metadata to it would weaken request-key validation.
    if isinstance(page, MutableMapping):
        page["_source_primary_sha256"] = str(source_binding["sha256"])
    # The upstream linear packet may expose only source-primary handles in
    # ``_provider_message_handles`` even though substantive turns were kept
    # in its adjacent window.  Build a deterministic union so every eligible
    # semantic unit reaches the provider projection, while retaining the
    # compact v3 hard cap of fourteen message rows.
    message_handles: List[str] = []
    for values in (
        page.get("primary_message_handles", ()),
        page.get("_provider_message_handles", ()),
        page.get("adjacent_message_handles", ()),
        page.get("message_handles", ()),
    ):
        for value in _safe_handle_values(values):
            if value and value not in message_handles:
                message_handles.append(str(value))
    if not message_handles:
        for row in _rows(page.get("message_rows")) + _rows(page.get("messages")):
            value = _id_from_row(row, "message_handle", "message_id", "handle", "id")
            if value and value not in message_handles:
                message_handles.append(value)
    message_handles = message_handles[:14]
    candidate_handles = [str(value) for value in page.get("candidate_handles", page.get("candidate_link_refs", ())) if value not in (None, "")]
    messages: List[Dict[str, Any]] = []
    hint_messages: List[Dict[str, Any]] = []
    role_infos: List[Mapping[str, Any]] = []
    seen_message_ids: set[str] = set()
    for handle in message_handles:
        row = _find_store_row(store, handle, "m")
        text = _extract_text(row, store)
        if not row:
            # Synthetic callers may provide a body-bearing row alongside the
            # page instead of a central store.  It remains in memory only and
            # is compacted into the bounded cue below.
            for candidate in _rows(page.get("message_rows")) + _rows(page.get("messages")):
                candidate_handle = _id_from_row(candidate, "message_handle", "message_id", "handle", "id")
                if candidate_handle == handle:
                    row = candidate
                    text = _extract_text(row, store)
                    break
        # Different K2/K10 views can carry several aliases/fragments for one
        # authoritative message.  Resolve the row before building the wire
        # so one message contributes at most one provider cue.
        # Prefer an immutable message identity from nested authority wrappers
        # before falling back to a fragment/transport handle.  K2/K10 can
        # expose one message through multiple handles; only one provider row
        # should survive that local projection.
        message_identity = _message_identity_for_dedup(row)
        if message_identity and message_identity.casefold() != "unknown" and message_identity in seen_message_ids:
            continue
        if message_identity and message_identity.casefold() != "unknown":
            seen_message_ids.add(message_identity)
        role_info = _projected_message_role(row, handle, page, root, text=text, store=store)
        role_infos.append(role_info)
        projected_role = str(role_info["projected_role"])
        # The protocol builder performs a second local role check.  Feed it
        # the already resolved semantic bit so stale source labels (for
        # example fragment_type=acknowledgement on a mixed question) cannot
        # undo a safe adjacent promotion, and a hard context barrier cannot
        # be laundered into a primary by the second pass.
        provider_message_type = row.get("message_type", row.get("type", "text")) if isinstance(row, Mapping) else "text"
        # The v3 wire has no role metadata beyond ``r``.  Feed a typed
        # context marker to the local protocol builder for rows already
        # rejected by the authoritative projection; otherwise its generic
        # lexical fallback could re-promote a context cue while constructing
        # the same wire request.  The marker is not emitted on the wire.
        # Keep the second (legacy/unreachable) builder in lock-step with the
        # selected path: a context row must remain r=c even when it has a
        # direct caption or an authority overlap.
        if projected_role != "primary":
            provider_message_type = "system"
        provider_message = {
            "handle": handle,
            "role": projected_role,
            "text": text,
            "message_type": provider_message_type,
            "semantic_role": "substantive" if projected_role == "primary" else "context",
        }
        # The context-packet merge carries a body-free direct-caption marker
        # for the intentionally narrow event/system exception.  Preserve it
        # as a caption field in this in-memory provider row; an ordinary
        # system ``text`` remains context-only in the v3 protocol gate.
        if (
            isinstance(row, Mapping)
            and (
                row.get("_context_direct_caption") is True
                or any(
                    row.get(key) not in (None, "")
                    for key in ("caption", "text_redacted", "message_text")
                )
            )
            and text
            and provider_message_type in {"system", "event", "event_message"}
        ):
            provider_message["caption"] = text
            # The protocol gate applies the same narrow event/system-caption
            # rule as the projection.  Carry only fixed provenance labels in
            # this in-memory row so an accepted direct caption is not demoted
            # by the second pass; no source body or arbitrary metadata is
            # copied to the wire.
            if role_info.get("projected_role") == "primary" and role_info.get("direct_human_caption"):
                provider_message["roles"] = ["primary", "authority", "substantive"]
        messages.append(provider_message)
        # Preserve source metadata only for the in-memory deterministic
        # preflight.  ``build_topic_candidate_hints`` reads narrow identity /
        # subject / object / action / state / reply fields and emits aliases
        # plus fixed relation labels; no row body can reach the wire.
        hint_record = dict(row) if isinstance(row, Mapping) else {"handle": handle}
        hint_record.update({
            "i": "m%d" % len(messages),
            "k": "m",
            "h": handle,
            "r": "p" if projected_role == "primary" else "c",
        })
        hint_messages.append(hint_record)
    candidates = [{"handle": handle} for handle in candidate_handles]
    hint_candidates: List[Dict[str, Any]] = []
    for index, handle in enumerate(candidate_handles, 1):
        candidate_row = _find_store_row(store, handle, "c")
        hint_record = dict(candidate_row) if isinstance(candidate_row, Mapping) else {"handle": handle}
        hint_record.update({"i": "c%d" % index, "k": "c", "h": handle})
        hint_candidates.append(hint_record)
    topic_hints = build_topic_candidate_hints(hint_messages, hint_candidates)
    # Capture body-free role telemetry before protocol validation.  This also
    # makes an all-context/unbound page diagnosable when the strict request
    # builder rejects it for lacking a primary alias.
    provisional_request = {
        "h": [
            {
                "k": "m",
                "i": str(item.get("handle", "")),
                "r": "p" if str(item.get("role")) == "primary" else "c",
            }
            for item in messages
        ]
    }
    if isinstance(page, MutableMapping):
        page["_role_projection_telemetry"] = _role_projection_telemetry(
            page,
            provisional_request,
            role_infos,
            root=root,
        )
    try:
        ceiling = "uncertain" if _page_has_unresolved_pronoun_candidate(page, root, store) else None
        # ``topic_hints`` was produced by the body-free preflight above, so it
        # is safe to remove only its deterministic tail when the complete HTTP
        # envelope is unusually long.  This is not applied to caller-supplied
        # protocol packets: explicit ``g`` remains strict and un-repaired at
        # the protocol boundary.
        fitted_topic_hints = list(topic_hints)
        while True:
            try:
                request = build_compact_stage_a_request(
                    scope,
                    messages,
                    candidates,
                    uncertainty_ceiling=ceiling,
                    topic_hints=fitted_topic_hints,
                )
                break
            except CompactStageAProtocolV3Error as exc:
                if exc.code != "request_input_token_proxy_exceeded" or not fitted_topic_hints:
                    raise
                fitted_topic_hints.pop()

        if isinstance(page, MutableMapping):
            page["_source_primary_eligible"] = bool(
                any(row.get("k") == "m" and row.get("r") == "p" for row in request.get("h", ()) if isinstance(row, Mapping))
                and source_binding["provided"]
                and all(bool(info.get("authoritative_message_bound")) for info in role_infos if isinstance(info, Mapping) and info.get("projected_role") == "primary")
            )
            page["_role_projection_telemetry"] = _role_projection_telemetry(
                page,
                request,
                role_infos,
                root=root,
            )
        return request
    except Exception as exc:
        if isinstance(page, MutableMapping):
            page["_source_primary_eligible"] = False
        code = str(getattr(exc, "code", "page_request_invalid"))
        raise CompactStageADevelopmentError(code) from exc


def _normalise_metadata_label(value: Any) -> str:
    """Normalise a body-free category/view label without inspecting text."""

    if isinstance(value, bool) or value in (None, ""):
        return ""
    label = str(value).strip().casefold()
    label = re.sub(r"^(?:category|stratum)\s*[:=]\s*", "", label)
    label = label.replace("-", "_").replace(" ", "_")
    return re.sub(r"_+", "_", label)


def _canonical_stratum(value: Any) -> str:
    label = _normalise_metadata_label(value)
    if label.endswith("_stratum"):
        label = label[: -len("_stratum")]
    aliases = {
        "candidate_competition": "candidate_competition",
        "candidate_competition_high": "candidate_competition",
        "competition": "candidate_competition",
        "competing_candidates": "candidate_competition",
        "pronoun_person_object_state": "pronoun_person_object_state",
        "person_object_state": "pronoun_person_object_state",
        "pronoun_person_object": "pronoun_person_object_state",
        "greeting_new_topic": "greeting_new_topic",
        "greeting": "greeting_new_topic",
        "conversation_opener": "greeting_new_topic",
        "opener": "greeting_new_topic",
        "new_topic": "greeting_new_topic",
        "topic_shift": "topic_shift",
        "topic_change": "topic_shift",
        "shift": "topic_shift",
        "no_reply": "no_reply",
        "no_reply_stratum": "no_reply",
        "no_reply_relation": "no_reply",
        "unanswered": "no_reply",
        "no_reply": "no_reply",
    }
    return aliases.get(label, "")


def _metadata_values(value: Any) -> Iterable[Any]:
    """Yield shallow metadata values; never recurse into arbitrary payloads."""

    if isinstance(value, Mapping):
        for key, enabled in value.items():
            if enabled is True or enabled == 1 or str(enabled).casefold() in {"true", "yes", "on"}:
                yield key
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        yield from value
        return
    if isinstance(value, str):
        yield value


def _explicit_strata(source: Any) -> set[str]:
    """Read only explicit canonical stratum fields from one metadata row."""

    if not isinstance(source, Mapping):
        return set()
    output: set[str] = set()
    for raw_key, value in source.items():
        key = _normalise_metadata_label(raw_key)
        if key in _STRATUM_METADATA_KEYS:
            for candidate in _metadata_values(value):
                mapped = _canonical_stratum(candidate)
                if mapped:
                    output.add(mapped)
        elif key in _STRATUM_DIRECT_KEYS:
            enabled = value is True or value == 1 or (isinstance(value, str) and value.casefold() in {"true", "yes", "on"})
            if enabled:
                mapped = _canonical_stratum(key)
                if mapped:
                    output.add(mapped)
        elif key in _STRATUM_SIGNAL_KEYS:
            # Signal fields are accepted only when their value is itself an
            # explicit canonical label.  Weak proximity/continuity reasons
            # therefore cannot become no_reply or competition by implication.
            for candidate in _metadata_values(value):
                mapped = _canonical_stratum(candidate)
                if mapped:
                    output.add(mapped)
    return output


def _candidate_kind(value: Any) -> str:
    label = _normalise_metadata_label(value)
    if "candidate_person_history" in label or "person_history" in label or "person_history" in label:
        return "person"
    if "candidate_object_history" in label or "object_history" in label:
        return "object"
    if "candidate_state_history" in label or "state_history" in label:
        return "state"
    return ""


def _safe_handle_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple, set, frozenset)):
        for child in value:
            if child not in (None, ""):
                yield str(child)


def _selection_metadata_rows(page: Mapping[str, Any], store: Optional[Mapping[str, Any]]) -> Tuple[List[Mapping[str, Any]], List[Mapping[str, Any]]]:
    """Return candidate/message metadata rows while excluding body-shaped fields."""

    candidates = list(_safe_handle_values(page.get("candidate_handles", page.get("candidate_link_refs", ()))))
    messages = list(_safe_handle_values(page.get("message_handles", page.get("primary_message_handles", ()))))
    candidate_rows = [_find_store_row(store, handle, "c") for handle in candidates]
    message_rows = [_find_store_row(store, handle, "m") for handle in messages]
    materialized = page.get("_materialized_meta")
    if isinstance(materialized, Mapping):
        for row in _rows(materialized.get("candidate_rows")):
            candidate_rows.append(row)
        for row in _rows(materialized.get("message_rows")):
            message_rows.append(row)
    return [row for row in candidate_rows if isinstance(row, Mapping)], [row for row in message_rows if isinstance(row, Mapping)]


def _candidate_history_kinds(page: Mapping[str, Any], candidate_rows: Sequence[Mapping[str, Any]], root: Mapping[str, Any]) -> set[str]:
    kinds: set[str] = set()
    for row in candidate_rows:
        for key in ("view_names", "views", "candidate_reason", "reason_codes", "relation_subtype"):
            for value in _metadata_values(row.get(key)):
                kind = _candidate_kind(value)
                if kind:
                    kinds.add(kind)
        for key in ("candidate_handle", "handle", "id"):
            kind = _candidate_kind(row.get(key))
            if kind:
                kinds.add(kind)
    candidate_views = root.get("candidate_views") if isinstance(root, Mapping) else None
    if isinstance(candidate_views, Mapping):
        page_handles = set(_safe_handle_values(page.get("candidate_handles", page.get("candidate_link_refs", ()))))
        for key, values in candidate_views.items():
            value_handles = set(_safe_handle_values(values))
            # Root-level candidate_views may contain candidates belonging to
            # other pages.  Attribute a history kind to this page only when a
            # referenced opaque handle is actually present on the page.
            if page_handles and value_handles.intersection(page_handles):
                kind = _candidate_kind(key)
                if kind:
                    kinds.add(kind)
                for value in value_handles:
                    kind = _candidate_kind(value)
                    if kind:
                        kinds.add(kind)
    else:
        for row in _rows(candidate_views):
            for key in ("view_names", "views", "candidate_reason", "reason_codes"):
                for value in _metadata_values(row.get(key)):
                    kind = _candidate_kind(value)
                    if kind:
                        kinds.add(kind)
    return kinds


def _message_derived_strata(message_rows: Sequence[Mapping[str, Any]]) -> set[str]:
    derived: set[str] = set()
    identities: List[Mapping[str, Any]] = []
    for row in message_rows:
        identity = row.get("identity_row") if isinstance(row.get("identity_row"), Mapping) else row
        if isinstance(identity, Mapping):
            identities.append(identity)
        derived.update(_explicit_strata(row))
        if isinstance(identity, Mapping) and identity is not row:
            derived.update(_explicit_strata(identity))
    for row in identities:
        fragment = _normalise_metadata_label(row.get("fragment_type"))
        if bool(row.get("is_opener")) or bool(row.get("new_topic")) or fragment in {"greeting", "conversation_opener", "opener", "new_topic"}:
            derived.add("greeting_new_topic")
        if bool(row.get("topic_shift")) or bool(row.get("topic_change")) or fragment in {"topic_shift", "topic_change", "shift"}:
            derived.add("topic_shift")
    segment_ids = {str(row.get("segment_id")) for row in identities if row.get("segment_id") not in (None, "")}
    if len(segment_ids) > 1:
        derived.add("topic_shift")
    if any(row.get("reply_count") == 0 for row in identities):
        derived.add("no_reply")
    return derived


def _page_stratum_evidence(page: Mapping[str, Any], store: Optional[Mapping[str, Any]]) -> Tuple[Tuple[str, ...], str]:
    """Classify a page once, conservatively, from body-free metadata."""

    materialized = page.get("_materialized_meta")
    root = _store_root(store, str(page.get("root_id", "")))
    explicit: set[str] = set(_explicit_strata(page))
    if isinstance(materialized, Mapping):
        explicit.update(_explicit_strata(materialized))
    explicit.update(_explicit_strata(root))
    candidate_rows, message_rows = _selection_metadata_rows(page, store)
    for row in candidate_rows:
        explicit.update(_explicit_strata(row))
    kinds = _candidate_history_kinds(page, candidate_rows, root)
    derived: set[str] = set(_message_derived_strata(message_rows))
    if {"person", "object", "state"} <= kinds:
        derived.add("pronoun_person_object_state")
    # Page-level fields may be emitted by a body-free upstream selector.
    if page.get("topic_shift") is True or page.get("topic_change") is True:
        derived.add("topic_shift")
    if page.get("new_topic") is True or page.get("is_opener") is True:
        derived.add("greeting_new_topic")
    if page.get("no_reply") is True or page.get("reply_count") == 0:
        derived.add("no_reply")
    all_labels = explicit | derived
    ordered = tuple(name for name in CATEGORY_NAMES if name in all_labels)
    if len(ordered) > 1:
        return (), "ambiguous"
    if len(ordered) == 1:
        return ordered, "explicit" if explicit else "derived"
    return (), "metadata_missing"


def _page_flags(page: Mapping[str, Any], store: Optional[Mapping[str, Any]]) -> Tuple[str, ...]:
    """Return at most one canonical stratum; never infer from volume/absence."""

    return _page_stratum_evidence(page, store)[0]


def _page_complete(page: Mapping[str, Any], materialized: Optional[Mapping[str, Any]]) -> bool:
    if materialized is None:
        return str(page.get("status", "complete")) == "complete"
    if materialized.get("status") != "complete":
        return False
    stage_a = materialized.get("stage_a")
    if isinstance(stage_a, Mapping) and stage_a.get("status") != "complete":
        return False
    if materialized.get("stage_b_status") not in (None, "N/A") or materialized.get("stage_c_status") not in (None, "N/A"):
        raise CompactStageADevelopmentError("stage_b_c_forbidden")
    if materialized.get("within_limits") is False:
        return False
    return True


def _project_selection_row(value: Any) -> Mapping[str, Any]:
    """Project a metadata row without copying body-bearing fields."""

    if not isinstance(value, Mapping):
        return {}
    output: Dict[str, Any] = {}
    for raw_key, child in value.items():
        key = str(raw_key)
        folded = key.casefold()
        if folded not in _SELECTION_SAFE_FIELDS or folded in _BODY_KEYS:
            continue
        if isinstance(child, Mapping):
            nested = _project_selection_row(child)
            if nested:
                output[key] = nested
        elif isinstance(child, (list, tuple, set, frozenset)):
            # Lists are retained only as scalar metadata or recursively
            # projected mappings.  Text-like values are intentionally not
            # copied even if a producer used an ambiguous field name.
            values: List[Any] = []
            for item in child:
                if isinstance(item, Mapping):
                    projected = _project_selection_row(item)
                    if projected:
                        values.append(projected)
                elif not isinstance(item, str) or folded in {
                    "view_names",
                    "views",
                    "candidate_reason",
                    "candidate_reasons",
                    "reason_code",
                    "reason_codes",
                    "reasons",
                    "roles",
                } or folded in {
                    "source_primary_message_ids",
                    "source_primary_message_handles",
                    "source_primary_ids",
                    "source_primary_handles",
                    "primary_message_ids",
                    "primary_message_handles",
                    "primary_ids",
                    "primary_handles",
                    "authority_message_ids",
                    "authority_message_handles",
                    "authority_ids",
                    "authority_handles",
                    "adjacent_message_ids",
                    "adjacent_message_handles",
                    "context_message_ids",
                    "context_message_handles",
                    "context_ids",
                    "context_handles",
                }:
                    values.append(item)
                elif folded in {"categories", "category_flags", "selection_categories", "selection_strata", "strata"}:
                    values.append(item)
            if values:
                output[key] = values
        elif not isinstance(child, str) or folded in {
            "fragment_type",
            "role",
            "layer",
            "message_role",
            "view_names",
            "views",
            "candidate_reason",
            "candidate_reasons",
            "reason_code",
            "reason_codes",
            "reasons",
            "relation_subtype",
            "category",
            "categories",
            "selection_category",
            "selection_categories",
            "selection_stratum",
            "selection_strata",
            "stratum",
            "strata",
            "segment_id",
            "reply_to_message_id",
            "quote_message_id",
        }:
            output[key] = child
    return output


def _project_selection_metadata(*sources: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Keep only shallow, body-free selection metadata from materialized rows."""

    output: Dict[str, Any] = {}
    message_rows: List[Mapping[str, Any]] = []
    candidate_rows: List[Mapping[str, Any]] = []
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for raw_key, value in source.items():
            key = str(raw_key)
            folded = key.casefold()
            if folded in {"messages", "message_table", "message_rows"}:
                message_rows.extend(_project_selection_row(row) for row in _rows(value))
                continue
            if folded in {"candidates", "candidate_table", "candidate_rows"}:
                candidate_rows.extend(_project_selection_row(row) for row in _rows(value))
                continue
            if folded not in _SELECTION_META_KEYS:
                continue
            projected = _project_selection_row({key: value})
            if projected:
                output[key] = projected[key]
    message_rows = [row for row in message_rows if row]
    candidate_rows = [row for row in candidate_rows if row]
    if message_rows:
        output["message_rows"] = message_rows
    if candidate_rows:
        output["candidate_rows"] = candidate_rows
    return output


def _normalize_mapping_pages(pages: Sequence[Any], store: Optional[Mapping[str, Any]] = None) -> Tuple[List[Dict[str, Any]], Mapping[str, Any]]:
    output: List[Dict[str, Any]] = []
    for raw in pages:
        if not isinstance(raw, Mapping):
            raise CompactStageADevelopmentError("input_artifact_invalid")
        if isinstance(raw.get("page"), Mapping):
            page = dict(raw["page"])
            materialized = raw.get("materialized") if isinstance(raw.get("materialized"), Mapping) else raw.get("_materialized") if isinstance(raw.get("_materialized"), Mapping) else None
        else:
            page = dict(raw)
            materialized = page.get("materialized") if isinstance(page.get("materialized"), Mapping) else page.get("_materialized") if isinstance(page.get("_materialized"), Mapping) else None
        if not page.get("page_id") or not page.get("root_id"):
            raise CompactStageADevelopmentError("input_artifact_invalid")
        if not _page_complete(page, materialized):
            continue
        if "scope" not in page:
            raise CompactStageADevelopmentError("scope_invalid")
        page["_materialized_meta"] = {"status": "complete"} if materialized is None else {
            "status": materialized.get("status"),
            "stage_a_status": (materialized.get("stage_a") or {}).get("status") if isinstance(materialized.get("stage_a"), Mapping) else None,
        }
        if materialized is not None:
            stage_a = materialized.get("stage_a") if isinstance(materialized.get("stage_a"), Mapping) else None
            page["_materialized_meta"].update(_project_selection_metadata(materialized, stage_a))
        output.append({"page": page, "materialized": materialized})
    if not output:
        raise CompactStageADevelopmentError("input_artifact_missing")
    return output, store or {}


def _read_v2_input(root: Union[str, Path]) -> Tuple[List[Dict[str, Any]], Mapping[str, Any], Mapping[str, Any], Dict[str, str]]:
    input_root = _safe_path(root, "input_artifact_invalid")
    if input_root.name.casefold() != LEGACY_INPUT_ARTIFACT_VERSION.casefold() or not input_root.is_dir():
        raise CompactStageADevelopmentError("input_artifact_invalid")
    manifest_path = input_root / "manifest.private.json"
    pages_path = input_root / "pages.private.jsonl"
    materialized_path = input_root / "materialized_map.private.jsonl"
    store_path = input_root / "store.private.json"
    if not all(path.is_file() for path in (manifest_path, pages_path, materialized_path, store_path)):
        raise CompactStageADevelopmentError("input_artifact_missing")
    manifest = _json_read(manifest_path, "input_manifest_invalid")
    if manifest.get("artifact_version") != LEGACY_INPUT_ARTIFACT_VERSION or manifest.get("split") not in (None, "development"):
        raise CompactStageADevelopmentError("input_manifest_invalid")
    if manifest.get("local_day") not in (None, LOCAL_DAY) or manifest.get("frozen_read") is True:
        raise CompactStageADevelopmentError("input_manifest_invalid")
    if manifest.get("provider_called") is True or int(manifest.get("provider_calls") or 0) != 0 or manifest.get("gold_loaded") is True:
        raise CompactStageADevelopmentError("input_manifest_invalid")
    pages = _jsonl_read(pages_path, "input_artifact_invalid")
    materialized = _jsonl_read(materialized_path, "input_artifact_invalid")
    store = _json_read(store_path, "input_artifact_invalid")
    material_by_page = {str(row.get("page_id")): row for row in materialized}
    rows = []
    for page in pages:
        page_id = str(page.get("page_id") or "")
        item = dict(page)
        item["_materialized"] = material_by_page.get(page_id)
        rows.append(item)
    normalized, _ = _normalize_mapping_pages(rows, store)
    hashes = {
        "manifest_sha256": _file_sha256(manifest_path),
        "pages_sha256": _file_sha256(pages_path),
        "materialized_sha256": _file_sha256(materialized_path),
        "store_sha256": _file_sha256(store_path),
    }
    return normalized, store, manifest, hashes


_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _require_sha256(value: Any, code: str) -> str:
    text = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(text):
        raise CompactStageADevelopmentError(code)
    return text


def _verify_artifact_hashes(
    root: Path,
    manifest: Mapping[str, Any],
    filenames: Sequence[str],
    *,
    prefix: str,
) -> Dict[str, str]:
    """Verify the manifest's immutable file hashes before using an artifact."""

    expected = manifest.get("artifact_hashes")
    if not isinstance(expected, Mapping):
        raise CompactStageADevelopmentError("input_manifest_invalid")
    result: Dict[str, str] = {
        prefix + "manifest_sha256": _require_sha256(_file_sha256(root / "manifest.private.json"), "input_hash_mismatch")
    }
    for filename in filenames:
        path = root / filename
        if not path.is_file():
            raise CompactStageADevelopmentError("input_artifact_missing")
        actual = _require_sha256(_file_sha256(path), "input_hash_mismatch")
        recorded = _require_sha256(expected.get(filename), "input_manifest_invalid")
        if actual != recorded:
            raise CompactStageADevelopmentError("input_hash_mismatch")
        result[prefix + filename.replace(".", "_") + "_sha256"] = actual
    return result


def _assert_body_free_rows(rows: Iterable[Mapping[str, Any]], code: str = "input_artifact_invalid") -> None:
    """Fail closed if a supposedly ledger-only row contains a body field."""

    try:
        for row in rows:
            _body_free(row)
    except CompactStageADevelopmentError:
        raise CompactStageADevelopmentError(code)


def _read_context_packet_current(root: Union[str, Path]) -> Tuple[Mapping[str, Any], Dict[str, Mapping[str, Any]], Dict[str, str]]:
    """Read the K2 current packet corpus and retain bodies only in memory."""

    context_root = _safe_path(root, "context_input_invalid")
    manifest_path = context_root / "manifest.private.json"
    packets_path = context_root / "packets.private.jsonl"
    required = (
        "aggregate.private.json",
        "audit_queue.private.jsonl",
        "cost.private.json",
        "errors.private.jsonl",
        "packets.private.jsonl",
        "selection_map.private.jsonl",
    )
    if not manifest_path.is_file() or not all((context_root / name).is_file() for name in required):
        raise CompactStageADevelopmentError("context_input_invalid")
    manifest = _json_read(manifest_path, "context_input_invalid")
    if manifest.get("artifact_version") != CONTEXT_INPUT_ARTIFACT_VERSION:
        raise CompactStageADevelopmentError("context_input_invalid")
    if manifest.get("split") not in (None, "development") or manifest.get("local_day") not in (None, LOCAL_DAY):
        raise CompactStageADevelopmentError("context_input_invalid")
    if manifest.get("frozen_read") is True or manifest.get("gold_loaded") is True or manifest.get("provider_called") is True:
        raise CompactStageADevelopmentError("context_input_invalid")
    try:
        if int(manifest.get("provider_calls") or 0) != 0:
            raise CompactStageADevelopmentError("context_input_invalid")
    except (TypeError, ValueError) as exc:
        raise CompactStageADevelopmentError("context_input_invalid") from exc
    _assert_body_free_rows((manifest,), "context_input_invalid")
    hashes = _verify_artifact_hashes(context_root, manifest, required, prefix="context_")
    packets = _jsonl_read(packets_path, "context_input_invalid")
    packet_map: Dict[str, Mapping[str, Any]] = {}
    for packet in packets:
        packet_id = str(packet.get("packet_id") or packet.get("context_packet_id") or "")
        if not packet_id or packet_id in packet_map:
            raise CompactStageADevelopmentError("context_input_invalid")
        # Packet bodies are intentionally read for the in-memory request and
        # are never sent to the artifact writer.  Do not call _body_free here.
        packet_map[packet_id] = packet
    hashes["context_input_sha256"] = _require_sha256(manifest.get("input_sha256"), "context_input_invalid")
    return manifest, packet_map, hashes


def _audit_paths(root: Union[str, Path]) -> Tuple[Path, Path]:
    audit_root = _safe_path(root, "audit_invalid")
    if audit_root.suffix.casefold() == ".json":
        return audit_root, audit_root.with_name("human_audit.private.jsonl")
    return audit_root / "audit_summary.private.json", audit_root / "human_audit.private.jsonl"


def _validate_current_linear_audit_root(root: Union[str, Path]) -> Path:
    """Accept only the audit sidecar for the current linear artifact.

    A historical ``artifact/audit`` directory may contain byte-for-byte
    equivalent-looking records, but it is not the source selected by the
    current linear artifact and therefore cannot authorize a repair run.  The
    basename check intentionally permits temporary copies used by synthetic
    tests while still requiring the explicit current-artifact directory name.
    """

    audit_root = _safe_path(root, "audit_invalid")
    candidate = audit_root.parent if audit_root.suffix.casefold() == ".json" else audit_root
    if candidate.name.casefold() != "audit" or candidate.parent.name.casefold() != INPUT_ARTIFACT_VERSION.casefold():
        raise CompactStageADevelopmentError("audit_invalid")
    return candidate


def _read_current_audit(
    root: Union[str, Path],
    selected_entries: Sequence[Mapping[str, Any]],
    *,
    summary_override: Optional[Mapping[str, Any]] = None,
    human_override: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Tuple[Mapping[str, Any], List[Mapping[str, Any]], Dict[str, str]]:
    """Validate the current Stage-A-only audit sidecars and selected refs."""

    _validate_current_linear_audit_root(root)
    summary_path, human_path = _audit_paths(root)
    if summary_override is None:
        if not summary_path.is_file():
            raise CompactStageADevelopmentError("audit_invalid")
        summary = _json_read(summary_path, "audit_invalid")
        summary_hash = _require_sha256(_file_sha256(summary_path), "audit_invalid")
    else:
        summary = dict(summary_override)
        summary_hash = stable_hash(summary)
    try:
        _body_free(summary)
    except CompactStageADevelopmentError as exc:
        raise CompactStageADevelopmentError("audit_invalid") from exc
    if summary.get("body_free") is False:
        raise CompactStageADevelopmentError("audit_invalid")
    if summary.get("status") != "stage_a_only_authorized":
        raise CompactStageADevelopmentError("audit_invalid")
    conclusion = summary.get("conclusion")
    if not isinstance(conclusion, Mapping):
        raise CompactStageADevelopmentError("audit_invalid")
    if conclusion.get("allow_stage_a_deepseek_up_to_5_pages") is not True or conclusion.get("stage_a_authorized") is not True:
        raise CompactStageADevelopmentError("audit_invalid")
    if str(conclusion.get("stage_a_variant", "")) != "v3" or int(conclusion.get("stage_a_page_limit") or 0) != MAX_SELECTED_PAGES:
        raise CompactStageADevelopmentError("audit_invalid")
    for key in ("stage_b_authorized", "stage_c_authorized", "production_authorized"):
        if conclusion.get(key) is True:
            raise CompactStageADevelopmentError("audit_invalid")
    scope_meta = summary.get("scope")
    if not isinstance(scope_meta, Mapping):
        raise CompactStageADevelopmentError("audit_invalid")
    if scope_meta.get("provider_called") is True or scope_meta.get("frozen_read") is True or scope_meta.get("gold_loaded") is True:
        raise CompactStageADevelopmentError("audit_invalid")
    try:
        if int(scope_meta.get("provider_calls") or 0) != 0:
            raise CompactStageADevelopmentError("audit_invalid")
    except (TypeError, ValueError) as exc:
        raise CompactStageADevelopmentError("audit_invalid") from exc
    for key in ("candidate_only", "not_canonical", "semantic_decision_pending"):
        if scope_meta.get(key) is not True:
            raise CompactStageADevelopmentError("audit_invalid")
    if scope_meta.get("selected_page_count") not in (None, MAX_SELECTED_PAGES):
        raise CompactStageADevelopmentError("audit_invalid")
    if scope_meta.get("selected_candidate_row_count") not in (None, MAX_SELECTED_PAGES):
        raise CompactStageADevelopmentError("audit_invalid")
    role_checks = summary.get("role_checks")
    if isinstance(role_checks, Mapping):
        for key in ("pure_confirmation_semantic_primary", "mixed_content_retention"):
            check = role_checks.get(key)
            if isinstance(check, Mapping) and str(check.get("verdict", "")).casefold() != "pass":
                raise CompactStageADevelopmentError("audit_invalid")

    selected_pages = summary.get("selected_pages")
    if not isinstance(selected_pages, list) or len(selected_pages) != MAX_SELECTED_PAGES:
        raise CompactStageADevelopmentError("audit_invalid")
    expected_refs = [
        (
            int(entry.get("selection_rank") or 0),
            str(entry.get("page_handle") or ""),
            str(entry.get("root_handle") or ""),
            str(entry.get("source_handle") or ""),
        )
        for entry in selected_entries
    ]
    observed_refs: List[Tuple[int, str, str, str]] = []
    for row in selected_pages:
        if not isinstance(row, Mapping):
            raise CompactStageADevelopmentError("audit_invalid")
        observed_refs.append(
            (
                int(row.get("selection_rank") or 0),
                str(row.get("page_ref") or row.get("page_handle") or ""),
                str(row.get("root_ref") or row.get("root_handle") or ""),
                str(row.get("source_ref") or row.get("source_handle") or ""),
            )
        )
    if observed_refs != expected_refs:
        raise CompactStageADevelopmentError("audit_invalid")

    if human_override is None:
        human_rows = _jsonl_read(human_path, "audit_invalid") if human_path.is_file() else []
        human_hash = _file_sha256(human_path) if human_path.is_file() else ""
        if human_hash:
            human_hash = _require_sha256(human_hash, "audit_invalid")
    else:
        human_rows = [dict(row) for row in human_override if isinstance(row, Mapping)]
        human_hash = stable_hash(human_rows)
    _assert_body_free_rows(human_rows, "audit_invalid")
    # A human audit may contain additional non-selected candidate rows.  Every
    # selected page, however, must have one matching body-free audit row.
    selected_row_refs = {(rank, page, root, source) for rank, page, root, source in expected_refs}
    selected_row_short_refs = {(rank, page) for rank, page, _, _ in expected_refs}
    matched: set[Tuple[int, str]] = set()
    for row in human_rows:
        rank = int(row.get("selection_rank") or 0)
        page = str(row.get("page_ref") or row.get("page_handle") or "")
        root = str(row.get("root_ref") or row.get("root_handle") or "")
        source = str(row.get("source_ref") or row.get("source_handle") or "")
        key = (
            rank,
            page,
            root,
            source,
        )
        if key in selected_row_refs:
            binding = row.get("evidence_binding")
            if isinstance(binding, Mapping):
                for marker in ("candidate_only", "not_canonical", "semantic_decision_pending"):
                    if binding.get(marker) is not True:
                        raise CompactStageADevelopmentError("audit_invalid")
            matched.add((rank, page))
        elif (rank, page) in selected_row_short_refs:
            # The compact human ledger may intentionally omit root/source
            # refs; rank + page ref still binds it to the audited selection.
            binding = row.get("evidence_binding")
            if isinstance(binding, Mapping):
                for marker in ("candidate_only", "not_canonical", "semantic_decision_pending"):
                    if binding.get(marker) is not True:
                        raise CompactStageADevelopmentError("audit_invalid")
            matched.add((rank, page))
    if matched != selected_row_short_refs:
        raise CompactStageADevelopmentError("audit_invalid")
    return summary, human_rows, {
        "audit_summary_sha256": summary_hash,
        "human_audit_sha256": human_hash or stable_hash(human_rows),
    }


_CONTEXT_EVENT_CAPTION_MESSAGE_TYPES = frozenset({"system", "event", "event_message"})
_CONTEXT_EVENT_CAPTION_FRAGMENT_TYPES = frozenset(
    {"event", "event_message", "event_caption", "caption"}
)


def _context_primary_event_caption(value: Mapping[str, Any]) -> bool:
    """Identify one bounded primary event caption without widening media rows.

    K2 can classify a real source-primary event caption as a ``media``
    fragment while its authoritative fact says ``system``.  Only a lexical
    cue with an explicit substantive/mixed/ellipsis semantic role qualifies;
    context-only, placeholder, empty, greeting, acknowledgement, and merged
    candidate rows remain ineligible.  The result is an in-memory marker used
    by the authority-bound projection and is never written to a public
    artifact.
    """

    if not isinstance(value, Mapping) or _has_merged_candidate_cue(value):
        return False
    semantic, raw, role_values = _role_label_sets((value,))
    labels = semantic | raw | role_values
    if not labels.intersection(_PROVIDER_SUBSTANTIVE_ROLES | {"ellipsis"}):
        return False
    message_type = _normalise_semantic_label(
        value.get("message_type", value.get("type", ""))
    )
    fragment_type = _normalise_semantic_label(
        value.get("fragment_type", value.get("kind", ""))
    )
    if (
        message_type not in _CONTEXT_EVENT_CAPTION_MESSAGE_TYPES
        and fragment_type not in _CONTEXT_EVENT_CAPTION_FRAGMENT_TYPES
    ):
        return False
    cue = value.get("text")
    return (
        isinstance(cue, str)
        and _semantic_cue_is_eligible(cue)
        and _record_has_substantive_signal(value, text=cue)
    )


def _context_message_index(packet: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    """Build an in-memory message/body index from one current K2 packet."""

    output: Dict[str, Mapping[str, Any]] = {}
    authoritative = {
        str(row.get("message_id")): row
        for row in _rows(packet.get("authoritative_facts"))
        if row.get("message_id") not in (None, "")
    }
    for row in _rows(packet.get("primary_fragments")):
        message_id = str(row.get("message_id") or "")
        if not message_id:
            continue
        item = dict(row)
        if isinstance(row.get("text_redacted"), str):
            item["text"] = row.get("text_redacted")
        auth = authoritative.get(message_id)
        if isinstance(auth, Mapping):
            item["message_type"] = auth.get("message_type", "text")
        if _context_primary_event_caption(item):
            # Preserve the fact that this is a direct caption even though its
            # K2 body arrived through the ordinary context overlay path.  The
            # projection will still require an authority-bound source-primary
            # role before allowing it onto the provider wire.
            item["_context_event_caption"] = True
        item["_context_priority"] = 3 if str(item.get("role", "")).casefold() not in {"context", "context_only", "adjacent"} else 2
        output[message_id] = item
    for row in _rows(packet.get("adjacent_context")):
        message_id = str(row.get("message_id") or "")
        if not message_id:
            continue
        item = dict(row)
        item.setdefault("role", "context_only")
        if isinstance(row.get("text_redacted"), str):
            item["text"] = row.get("text_redacted")
        auth = authoritative.get(message_id)
        if isinstance(auth, Mapping):
            item["message_type"] = auth.get("message_type", "text")
        item["_context_priority"] = 1
        existing = output.get(message_id)
        if not isinstance(existing, Mapping) or int(existing.get("_context_priority") or 0) < int(item["_context_priority"]):
            output[message_id] = item
    return output


def _merge_context_packet_bodies(store: Mapping[str, Any], packets: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any]:
    """Overlay current K2 body cues onto the K30 metadata store in memory."""

    if not packets:
        return store
    by_message: Dict[str, Mapping[str, Any]] = {}
    for packet in packets.values():
        for message_id, item in _context_message_index(packet).items():
            existing = by_message.get(message_id)
            if not isinstance(existing, Mapping) or int(existing.get("_context_priority") or 0) < int(item.get("_context_priority") or 0):
                by_message[message_id] = item
    messages = _table(store, "message_table", "messages")
    if not messages:
        return store
    merged_rows: List[Dict[str, Any]] = []
    for handle, raw in messages.items():
        row = dict(raw)
        message_id = str(row.get("message_id") or "")
        context = by_message.get(message_id)
        if isinstance(context, Mapping):
            if isinstance(context.get("text"), str):
                row["text"] = context["text"]
                row["_context_body_overlay"] = True
            if isinstance(context.get("caption"), str) and context.get("caption"):
                row["caption"] = context["caption"]
                row["_context_direct_caption"] = True
            if context.get("_context_event_caption") is True:
                row["_context_event_caption"] = True
                row["_context_direct_caption"] = True
            for key in ("fragment_type", "role", "is_silent", "message_type", "segment_id"):
                if context.get(key) not in (None, ""):
                    row[key] = context[key]
            # K2's adapter strips candidate cue bodies but retains this
            # scalar marker.  Carry only the marker across the in-memory
            # merge; otherwise the overlaid cue would look like a direct
            # message caption and could independently become provider
            # primary.
            if _has_merged_candidate_cue(context):
                row["merged_candidate_cue_present"] = True
        merged_rows.append(row)
    output = dict(store)
    output["messages"] = merged_rows
    output.pop("message_table", None)
    return output


def _stratified_plan(selected: Sequence[Tuple[Mapping[str, Any], Tuple[str, ...]]]) -> CompactStageASelectionPlan:
    """Represent the audited five-row global plan without reselecting pages."""

    scopes = {_scope_pair(page.get("scope")) for page, _ in selected}
    selected_scope = next(iter(scopes)) if len(scopes) == 1 else None
    available = tuple(category for category in CATEGORY_NAMES if any(category in categories for _, categories in selected))
    selected_counts = {
        category: sum(category in categories for _, categories in selected)
        for category in available
    }
    scope_coverage = {}
    if selected_scope is not None:
        label = _selection_scope_label(selected_scope)
        scope_coverage[label] = {
            "page_count": len(selected),
            "selectable_page_count": len(selected),
            "classified_page_count": len(selected),
            "available_strata": list(available),
            "stratum_page_counts": dict(selected_counts),
            "coverage_plan_page_ids": [str(page.get("page_id", "")) for page, _ in selected],
            "coverage_plan_available": False,
        }
    return CompactStageASelectionPlan(
        selected=tuple(selected),
        # The audited global plan is intentionally candidate-family scoped;
        # absence of canonical greeting/topic rows is not a reason to invent
        # additional pages or claim a sixth call.
        missing_strata=(),
        available_strata=available,
        page_count=len(selected),
        selectable_page_count=len(selected),
        selected_scope=selected_scope,
        selected_scope_count=len(scopes),
        scope_coverage=scope_coverage,
        stratum_page_counts=dict(selected_counts),
        selected_stratum_counts=dict(selected_counts),
        classification_counts={"explicit": len(selected), "derived": 0, "ambiguous": 0, "metadata_missing": 0},
        ambiguous_page_ids=(),
        unclassified_page_ids=(),
        global_coverage_plan_page_ids=tuple(str(page.get("page_id", "")) for page, _ in selected),
        global_coverage_plan_scope_count=len(scopes),
        global_coverage_plan_available=False,
        single_scope_coverage_plan_available=False,
        scope_authorization_required=False,
    )


def _read_stratified_current_input(
    root: Union[str, Path],
    context_root: Union[str, Path],
    audit_root: Union[str, Path],
    *,
    audit_summary: Optional[Mapping[str, Any]] = None,
    human_audit: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], Mapping[str, Any], Mapping[str, Any], Dict[str, str], Dict[str, Any], CompactStageASelectionPlan]:
    """Read and cross-check the fixed K30 selection + K2 bodies + audit."""

    input_root = _safe_path(root, "input_artifact_invalid")
    if not input_root.is_dir():
        raise CompactStageADevelopmentError("input_artifact_invalid")
    manifest_path = input_root / "manifest.private.json"
    required = (
        "aggregate.private.json",
        "audit.private.jsonl",
        "cost.private.json",
        "errors.private.jsonl",
        "materialized_map.private.jsonl",
        "pages.private.jsonl",
        "recovery_map.private.jsonl",
        "selection_map.private.jsonl",
        "store.private.json",
        "strata_map.private.jsonl",
    )
    if not manifest_path.is_file():
        raise CompactStageADevelopmentError("input_artifact_missing")
    manifest = _json_read(manifest_path, "input_manifest_invalid")
    if manifest.get("artifact_version") != STRATIFIED_SOURCE_ARTIFACT_VERSION:
        raise CompactStageADevelopmentError("input_manifest_invalid")
    if manifest.get("split") not in (None, "development") or manifest.get("local_day") not in (None, LOCAL_DAY):
        raise CompactStageADevelopmentError("input_manifest_invalid")
    if manifest.get("frozen_read") is True or manifest.get("gold_loaded") is True or manifest.get("provider_called") is True:
        raise CompactStageADevelopmentError("input_manifest_invalid")
    try:
        if int(manifest.get("provider_calls") or 0) != 0:
            raise CompactStageADevelopmentError("input_manifest_invalid")
    except (TypeError, ValueError) as exc:
        raise CompactStageADevelopmentError("input_manifest_invalid") from exc
    _assert_body_free_rows((manifest,), "input_manifest_invalid")
    hashes = _verify_artifact_hashes(input_root, manifest, required, prefix="source_")

    plan = manifest.get("candidate_selection_plan")
    if not isinstance(plan, Mapping):
        raise CompactStageADevelopmentError("selection_hash_missing")
    if plan.get("candidate_only") is not True or plan.get("not_canonical") is not True or plan.get("semantic_decision_pending") is not True:
        raise CompactStageADevelopmentError("candidate_only_required")
    selection_hash = _require_sha256(plan.get("selection_hash"), "selection_hash_missing")
    recomputed_plan = dict(plan)
    recomputed_plan.pop("selection_hash", None)
    if stable_hash(recomputed_plan) != selection_hash:
        raise CompactStageADevelopmentError("selection_hash_mismatch")
    global_plan = plan.get("global_candidate_plan")
    if not isinstance(global_plan, Mapping):
        raise CompactStageADevelopmentError("input_manifest_invalid")
    if global_plan.get("candidate_only") is not True or global_plan.get("not_canonical") is not True or global_plan.get("semantic_decision_pending") is not True:
        raise CompactStageADevelopmentError("candidate_only_required")
    if global_plan.get("coverage_kind") not in (None, "candidate_only"):
        raise CompactStageADevelopmentError("candidate_only_required")
    selected_entries = global_plan.get("selected")
    if not isinstance(selected_entries, list) or len(selected_entries) != MAX_SELECTED_PAGES:
        raise CompactStageADevelopmentError("input_manifest_invalid")
    selected_entries = sorted(selected_entries, key=lambda row: int(row.get("selection_rank") or 0) if isinstance(row, Mapping) else 0)
    if [int(row.get("selection_rank") or 0) for row in selected_entries if isinstance(row, Mapping)] != list(range(1, MAX_SELECTED_PAGES + 1)):
        raise CompactStageADevelopmentError("input_manifest_invalid")
    for row in selected_entries:
        if not isinstance(row, Mapping) or row.get("selected") is not True:
            raise CompactStageADevelopmentError("input_manifest_invalid")
        for key in ("page_handle", "root_handle", "source_handle", "scope_handle"):
            if not row.get(key):
                raise CompactStageADevelopmentError("input_manifest_invalid")
        if row.get("candidate_only") is not True or row.get("not_canonical") is not True or row.get("semantic_decision_pending") is not True:
            raise CompactStageADevelopmentError("candidate_only_required")
        cues = row.get("cue_families")
        if not isinstance(cues, list) or not cues:
            raise CompactStageADevelopmentError("input_manifest_invalid")
    if plan.get("selected_page_count") not in (None, MAX_SELECTED_PAGES) or global_plan.get("selected_page_count") not in (None, MAX_SELECTED_PAGES):
        raise CompactStageADevelopmentError("input_manifest_invalid")
    selected_scope_refs = {str(row.get("scope_handle")) for row in selected_entries}
    if len(selected_scope_refs) != 1:
        raise CompactStageADevelopmentError("candidate_scope_invalid")

    pages = _jsonl_read(input_root / "pages.private.jsonl", "input_artifact_invalid")
    materialized = _jsonl_read(input_root / "materialized_map.private.jsonl", "input_artifact_invalid")
    selection_rows = _jsonl_read(input_root / "selection_map.private.jsonl", "input_artifact_invalid")
    source_store = _json_read(input_root / "store.private.json", "input_artifact_invalid")
    _assert_body_free_rows(pages, "input_artifact_invalid")
    _assert_body_free_rows(materialized, "input_artifact_invalid")
    _assert_body_free_rows(selection_rows, "input_artifact_invalid")
    candidate_selection_rows = {
        (str(row.get("page_handle") or ""), str(row.get("root_handle") or ""), int(row.get("selection_rank") or 0)): row
        for row in selection_rows
        if row.get("selected") is True and str(row.get("selection_kind", "")).casefold() == "candidate_only"
    }
    expected_selection_keys = {
        (str(row.get("page_handle")), str(row.get("root_handle")), int(row.get("selection_rank")))
        for row in selected_entries
    }
    if set(candidate_selection_rows) != expected_selection_keys:
        raise CompactStageADevelopmentError("selection_hash_mismatch")
    for row in candidate_selection_rows.values():
        if row.get("candidate_only") is not True or row.get("not_canonical") is not True or row.get("semantic_decision_pending") is not True:
            raise CompactStageADevelopmentError("candidate_only_required")

    page_by_handle = {str(row.get("page_handle") or ""): row for row in pages}
    material_by_handle = {str(row.get("page_handle") or ""): row for row in materialized}
    roots = _rows(source_store.get("roots"))
    if len(roots) < MAX_SELECTED_PAGES:
        raise CompactStageADevelopmentError("input_artifact_invalid")
    context_manifest, context_packets, context_hashes = _read_context_packet_current(context_root)
    audit_summary, human_rows, audit_hashes = _read_current_audit(
        audit_root,
        selected_entries,
        summary_override=audit_summary,
        human_override=human_audit,
    )

    wrappers: List[Mapping[str, Any]] = []
    selected: List[Tuple[Mapping[str, Any], Tuple[str, ...]]] = []
    page_context_ids: Dict[str, str] = {}
    source_scopes: set[Tuple[str, str]] = set()

    def root_for_page(raw_page: Mapping[str, Any], position: int) -> Mapping[str, Any]:
        expected_message_count = int(raw_page.get("message_count") or 0)
        expected_candidate_count = int(raw_page.get("candidate_count") or 0)
        if position < len(roots):
            candidate = roots[position]
            if len(list(_safe_handle_values(candidate.get("message_handles")))) == expected_message_count and len(list(_safe_handle_values(candidate.get("candidate_handles")))) == expected_candidate_count:
                return candidate
        matches = [
            row
            for row in roots
            if len(list(_safe_handle_values(row.get("message_handles")))) == expected_message_count
            and len(list(_safe_handle_values(row.get("candidate_handles")))) == expected_candidate_count
        ]
        if len(matches) == 1:
            return matches[0]
        raise CompactStageADevelopmentError("input_artifact_invalid")

    for position, entry in enumerate(selected_entries):
        page_handle = str(entry.get("page_handle"))
        raw_page = page_by_handle.get(page_handle)
        material = material_by_handle.get(page_handle)
        if raw_page is None or material is None:
            raise CompactStageADevelopmentError("input_artifact_missing")
        if str(raw_page.get("root_handle") or "") != str(entry.get("root_handle")) or str(raw_page.get("source_handle") or "") != str(entry.get("source_handle")):
            raise CompactStageADevelopmentError("selection_hash_mismatch")
        if not _page_complete({"status": "complete"}, material):
            raise CompactStageADevelopmentError("complete_page_required")
        root = root_for_page(raw_page, position)
        context_id = str(root.get("source_packet_id") or root.get("packet_id") or root.get("root_id") or "")
        packet = context_packets.get(context_id)
        if packet is None:
            raise CompactStageADevelopmentError("context_packet_missing")
        page_scope = _scope(root.get("scope"))
        packet_scope = _scope(packet.get("scope", {"account_id": packet.get("account_id"), "chat_id": packet.get("chat_id")}))
        if _scope_pair(page_scope) != _scope_pair(packet_scope):
            raise CompactStageADevelopmentError("cross_chat_scope_violation")
        source_scopes.add(_scope_pair(page_scope))
        page = dict(raw_page)
        all_message_handles = list(_safe_handle_values(root.get("message_handles")))
        primary_message_handles = list(_safe_handle_values(root.get("primary_message_handles")))
        source_primary_message_ids = list(_safe_handle_values(root.get("source_primary_message_ids", root.get("primary_message_ids", ()))))
        source_primary_message_handles = list(_safe_handle_values(root.get("source_primary_message_handles", root.get("primary_message_handles", ()))))
        adjacent_message_handles = list(_safe_handle_values(root.get("adjacent_message_handles")))
        provider_message_handles: List[str] = []
        for handle in primary_message_handles + adjacent_message_handles + all_message_handles:
            if handle and handle not in provider_message_handles:
                provider_message_handles.append(handle)
            if len(provider_message_handles) >= 14:
                break
        page.update(
            {
                "page_id": page_handle,
                "root_id": str(entry.get("root_handle")),
                "source_packet_id": context_id,
                "scope": page_scope,
                "message_handles": all_message_handles,
                "_provider_message_handles": provider_message_handles,
                "primary_message_handles": primary_message_handles,
                "source_primary_message_ids": source_primary_message_ids,
                "source_primary_message_handles": source_primary_message_handles,
                "adjacent_message_handles": adjacent_message_handles,
                "adjacent_message_ids": list(_safe_handle_values(root.get("adjacent_message_ids", ()))),
                "authority_message_handles": list(_safe_handle_values(root.get("authority_message_handles"))),
                "authority_message_ids": list(_safe_handle_values(root.get("authority_message_ids", ()))),
                "candidate_handles": list(_safe_handle_values(root.get("candidate_handles"))),
                "status": "complete",
                "_store_root_id": context_id,
                "_selection_rank": int(entry.get("selection_rank")),
                "_source_handle": str(entry.get("source_handle")),
                "_scope_handle": str(entry.get("scope_handle")),
                "_candidate_only": True,
                "_not_canonical": True,
                "_semantic_decision_pending": True,
                "categories": [
                    _canonical_stratum(cue)
                    for cue in entry.get("cue_families", ())
                    if _canonical_stratum(cue)
                ],
            }
        )
        page["_materialized_meta"] = {"status": "complete", "stage_a_status": "complete"}
        page_context_ids[page_handle] = context_id
        selected.append((page, tuple(page.get("categories", ()))))
        wrappers.append({"page": page, "materialized": {"status": "complete", "stage_a": {"status": "complete"}, "stage_b_status": "N/A", "stage_c_status": "N/A", "within_limits": True}})
    if len(source_scopes) != 1:
        raise CompactStageADevelopmentError("cross_chat_scope_violation")

    merged_store = _merge_context_packet_bodies(source_store, context_packets)
    normalized, _ = _normalize_mapping_pages(wrappers, merged_store)
    # _normalize_mapping_pages returns fresh wrappers; selected rows are keyed
    # by the audited order and retain the internal store-root/context linkage.
    selected = [(item["page"], categories) for item, (_, categories) in zip(normalized, selected)]
    selection_plan_obj = _stratified_plan(selected)
    hashes.update(context_hashes)
    hashes.update(audit_hashes)
    lineage: Dict[str, Any] = {
        "stratified_current": True,
        "source_artifact_version": str(manifest.get("artifact_version")),
        "source_input_directory_name": input_root.name,
        "context_input_artifact_version": str(context_manifest.get("artifact_version")),
        "context_input_directory_name": _safe_path(context_root, "context_input_invalid").name,
        "selection_sha256": selection_hash,
        "context_input_sha256": hashes["context_input_sha256"],
        "audit_summary_sha256": hashes["audit_summary_sha256"],
        "human_audit_sha256": hashes["human_audit_sha256"],
        "selected_scope_ref": next(iter(selected_scope_refs)),
        "candidate_only": True,
        "not_canonical": True,
        "semantic_decision_pending": True,
        "audit_allow_stage_a": True,
        "provider_primary_role": "semantic_primary_only",
        "stage_b_authorized": False,
        "stage_c_authorized": False,
        "production_authorized": False,
        "selected_page_count": MAX_SELECTED_PAGES,
        "selected_context_packet_ids": list(page_context_ids.values()),
        "source_input_selected_digest": str(manifest.get("input_selected_digest") or ""),
        "source_input_packet_digest": str(manifest.get("input_packet_digest") or ""),
    }
    for key in ("source_input_selected_digest", "source_input_packet_digest"):
        if lineage[key]:
            _require_sha256(lineage[key], "input_manifest_invalid")
    return normalized, merged_store, manifest, hashes, lineage, selection_plan_obj


def _input_binding_hash(manifest: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], hashes: Mapping[str, str], store: Optional[Mapping[str, Any]] = None) -> str:
    metadata = {
        "artifact_version": manifest.get("artifact_version"),
        "local_day": manifest.get("local_day"),
        "split": manifest.get("split"),
        "input_selected_digest": manifest.get("input_selected_digest"),
        "source_hashes": dict(sorted((str(key), str(value)) for key, value in hashes.items())),
        "complete_page_refs": [
            {
                "page_id": str(item["page"].get("page_id", "")),
                "root_id": str(item["page"].get("root_id", "")),
                "page_hash": str(item["page"].get("page_hash", "")),
                "scope": _scope(item["page"].get("scope")),
                "categories": list(_page_flags(item["page"], store)),
            }
            for item in rows
        ],
    }
    return stable_hash(metadata)


def _selection_page_count(page: Mapping[str, Any], key: str) -> int:
    value = page.get(key, ())
    if isinstance(value, str):
        return 1 if value else 0
    if isinstance(value, (list, tuple, set, frozenset)):
        return sum(value_item not in (None, "") for value_item in value)
    return 0


def _selection_scope_label(scope: Tuple[str, str]) -> str:
    return f"{scope[0]}/{scope[1]}"


def _selection_items(rows: Sequence[Mapping[str, Any]], store: Optional[Mapping[str, Any]]) -> List[Tuple[Mapping[str, Any], Tuple[str, ...], str, Tuple[str, str]]]:
    decorated: List[Tuple[Mapping[str, Any], Tuple[str, ...], str, Tuple[str, str]]] = []
    seen: set[str] = set()
    for item in rows:
        if not isinstance(item, Mapping):
            raise CompactStageADevelopmentError("input_artifact_invalid")
        page = item.get("page") if isinstance(item.get("page"), Mapping) else item
        if not isinstance(page, Mapping):
            raise CompactStageADevelopmentError("input_artifact_invalid")
        page_id = str(page.get("page_id", ""))
        if not page_id or page_id in seen:
            continue
        seen.add(page_id)
        categories, reason = _page_stratum_evidence(page, store)
        decorated.append((page, categories, reason, _scope_pair(page.get("scope"))))
    return decorated


def _selection_order_key(item: Tuple[Mapping[str, Any], Tuple[str, ...], str, Tuple[str, str]]) -> Tuple[int, int, str]:
    page = item[0]
    return (
        -_selection_page_count(page, "candidate_handles"),
        -_selection_page_count(page, "message_handles"),
        str(page.get("page_id", "")),
    )


def _coverage_plan(items: Sequence[Tuple[Mapping[str, Any], Tuple[str, ...], str, Tuple[str, str]]]) -> List[Tuple[Mapping[str, Any], Tuple[str, ...]]]:
    """Select rare strata first, then fill the bounded page budget."""

    selected: List[Tuple[Mapping[str, Any], Tuple[str, ...]]] = []
    selected_ids: set[str] = set()
    available = {category for _, categories, _, _ in items for category in categories}
    covered: set[str] = set()
    while len(selected) < MAX_SELECTED_PAGES and available - covered:
        counts = {
            category: sum(category in categories for _, categories, _, _ in items)
            for category in available - covered
        }
        rarest = min(counts, key=lambda category: (counts[category], CATEGORY_NAMES.index(category)))
        candidates = [
            item
            for item in items
            if str(item[0].get("page_id", "")) not in selected_ids and rarest in item[1]
        ]
        candidates.sort(
            key=lambda item: (
                -len(set(item[1]) - covered),
                -_selection_page_count(item[0], "candidate_handles"),
                -_selection_page_count(item[0], "message_handles"),
                str(item[0].get("page_id", "")),
            )
        )
        if not candidates:
            break
        chosen = candidates[0]
        selected.append((chosen[0], chosen[1]))
        selected_ids.add(str(chosen[0].get("page_id", "")))
        covered.update(chosen[1])
    for item in sorted(items, key=_selection_order_key):
        if len(selected) >= MAX_SELECTED_PAGES:
            break
        page_id = str(item[0].get("page_id", ""))
        if page_id in selected_ids:
            continue
        selected.append((item[0], item[1]))
        selected_ids.add(page_id)
    return selected


def _selection_plan(rows: Sequence[Mapping[str, Any]], store: Optional[Mapping[str, Any]] = None) -> CompactStageASelectionPlan:
    items = _selection_items(rows, store)
    by_scope: Dict[Tuple[str, str], List[Tuple[Mapping[str, Any], Tuple[str, ...], str, Tuple[str, str]]]] = {}
    for item in items:
        by_scope.setdefault(item[3], []).append(item)
    global_plan = _coverage_plan(items)
    global_covered = {category for _, categories in global_plan for category in categories}
    global_plan_scopes = {
        _scope_pair(page.get("scope"))
        for page, _ in global_plan
    }
    scope_plans: Dict[Tuple[str, str], List[Tuple[Mapping[str, Any], Tuple[str, ...]]]] = {
        scope: _coverage_plan(scope_items) for scope, scope_items in by_scope.items()
    }
    target = set(CATEGORY_NAMES)
    full_scope_plans = {
        scope: plan
        for scope, plan in scope_plans.items()
        if target <= {category for _, categories in plan for category in categories}
    }
    if full_scope_plans:
        selected_scope = sorted(full_scope_plans, key=_selection_scope_label)[0]
    elif scope_plans:
        def scope_score(scope: Tuple[str, str]) -> Tuple[int, int, int, str]:
            plan = scope_plans[scope]
            covered = {category for _, categories in plan for category in categories}
            classified = sum(bool(categories) for page, categories in plan)
            return (-len(covered), -classified, -len(plan), _selection_scope_label(scope))

        selected_scope = sorted(scope_plans, key=scope_score)[0]
    else:
        selected_scope = None
    selected = tuple(scope_plans[selected_scope]) if selected_scope is not None else ()
    selected_covered = {category for _, categories in selected for category in categories}
    available = {category for _, categories, _, _ in items for category in categories}
    missing = tuple(category for category in CATEGORY_NAMES if category not in selected_covered)
    ordered_available = tuple(category for category in CATEGORY_NAMES if category in available)
    stratum_page_counts = {
        category: sum(category in categories for _, categories, _, _ in items)
        for category in CATEGORY_NAMES
        if category in available
    }
    selected_stratum_counts = {
        category: sum(category in categories for _, categories in selected)
        for category in CATEGORY_NAMES
        if category in selected_covered
    }
    classification_counts = {
        "explicit": sum(reason == "explicit" for _, _, reason, _ in items),
        "derived": sum(reason == "derived" for _, _, reason, _ in items),
        "ambiguous": sum(reason == "ambiguous" for _, _, reason, _ in items),
        "metadata_missing": sum(reason == "metadata_missing" for _, _, reason, _ in items),
    }
    ambiguous_page_ids = tuple(sorted(str(page.get("page_id", "")) for page, _, reason, _ in items if reason == "ambiguous"))
    unclassified_page_ids = tuple(sorted(str(page.get("page_id", "")) for page, _, reason, _ in items if reason in {"ambiguous", "metadata_missing"}))
    scope_coverage: Dict[str, Mapping[str, Any]] = {}
    for scope in sorted(by_scope, key=_selection_scope_label):
        scope_items = by_scope[scope]
        scope_plan = scope_plans[scope]
        scope_available = {category for _, categories, _, _ in scope_items for category in categories}
        scope_coverage[_selection_scope_label(scope)] = {
            "page_count": len(scope_items),
            "selectable_page_count": len(scope_items),
            "classified_page_count": sum(bool(categories) for _, categories, _, _ in scope_items),
            "available_strata": [category for category in CATEGORY_NAMES if category in scope_available],
            "stratum_page_counts": {
                category: sum(category in categories for _, categories, _, _ in scope_items)
                for category in CATEGORY_NAMES
                if category in scope_available
            },
            "coverage_plan_page_ids": [str(page.get("page_id", "")) for page, _ in scope_plan],
            "coverage_plan_available": target <= {category for _, categories in scope_plan for category in categories},
        }
    single_scope_available = bool(full_scope_plans)
    global_available = target <= global_covered
    scope_authorization_required = bool(global_available and not single_scope_available)
    return CompactStageASelectionPlan(
        selected=selected,
        missing_strata=missing,
        available_strata=ordered_available,
        page_count=len(items),
        selectable_page_count=len(items),
        selected_scope=selected_scope,
        selected_scope_count=len({_scope_pair(page.get("scope")) for page, _ in selected}),
        scope_coverage=scope_coverage,
        stratum_page_counts=stratum_page_counts,
        selected_stratum_counts=selected_stratum_counts,
        classification_counts=classification_counts,
        ambiguous_page_ids=ambiguous_page_ids,
        unclassified_page_ids=unclassified_page_ids,
        global_coverage_plan_page_ids=tuple(str(page.get("page_id", "")) for page, _ in global_plan),
        global_coverage_plan_scope_count=len(global_plan_scopes),
        global_coverage_plan_available=global_available,
        single_scope_coverage_plan_available=single_scope_available,
        scope_authorization_required=scope_authorization_required,
    )


def analyze_compact_stage_a_selection(rows: Sequence[Mapping[str, Any]], store: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Return an opaque, body-free selection diagnostic for offline audits."""

    return _selection_plan(rows, store).to_dict()


def _select_pages(rows: Sequence[Mapping[str, Any]], store: Optional[Mapping[str, Any]] = None) -> Tuple[List[Tuple[Mapping[str, Any], Tuple[str, ...]]], Tuple[str, ...]]:
    plan = _selection_plan(rows, store)
    return list(plan.selected), plan.missing_strata


def _body_free_output_metadata(value: Any) -> Tuple[int, str]:
    """Return scalar output length/hash without retaining provider text."""

    if isinstance(value, str):
        encoded = value.encode("utf-8")
        return len(value), hashlib.sha256(encoded).hexdigest()
    if isinstance(value, Mapping):
        try:
            encoded = canonical_json(value).encode("utf-8")
        except Exception:
            return 0, ""
        return len(encoded.decode("utf-8")), hashlib.sha256(encoded).hexdigest()
    return 0, ""


def _response_parts(value: Any, model: Any) -> Tuple[Any, int, int, float, str, str, str, int, str, int]:
    if isinstance(value, CompactStageAResponse):
        content_length, output_sha256 = _body_free_output_metadata(value.content)
        return value.content, max(0, int(value.input_tokens)), max(0, int(value.output_tokens)), max(0.0, float(value.latency_ms)), str(value.finish_reason or "stop"), str(value.model or MODEL_ID), str(value.source or SOURCE_ID), content_length, output_sha256, max(0, int(value.reasoning_length or 0))
    if isinstance(value, str):
        content_length, output_sha256 = _body_free_output_metadata(value)
        return value, 0, 0, 0.0, "stop", str(getattr(model, "model_id", MODEL_ID)), str(getattr(model, "source", SOURCE_ID)), content_length, output_sha256, 0
    if isinstance(value, Mapping):
        content = value.get(
            "content",
            value.get(
                "payload",
                value
                if any(key in value for key in ("topics", "guard_error_code", "error_code"))
                else None,
            ),
        )
        content_length, output_sha256 = _body_free_output_metadata(content)
        return content, max(0, int(value.get("input_tokens", value.get("prompt_tokens", 0)) or 0)), max(0, int(value.get("output_tokens", value.get("completion_tokens", 0)) or 0)), max(0.0, float(value.get("latency_ms", 0.0) or 0.0)), str(value.get("finish_reason", "stop") or "stop"), str(value.get("model") or getattr(model, "model_id", MODEL_ID)), str(value.get("source") or getattr(model, "source", SOURCE_ID)), int(value.get("content_length", content_length) or 0), str(value.get("output_sha256") or output_sha256), max(0, int(value.get("reasoning_length", 0) or 0))
    content = getattr(value, "content", getattr(value, "payload", None))
    content_length, output_sha256 = _body_free_output_metadata(content)
    return content, max(0, int(getattr(value, "input_tokens", 0) or 0)), max(0, int(getattr(value, "output_tokens", 0) or 0)), max(0.0, float(getattr(value, "latency_ms", 0.0) or 0.0)), str(getattr(value, "finish_reason", "stop") or "stop"), str(getattr(value, "model", getattr(model, "model_id", MODEL_ID))), str(getattr(value, "source", getattr(model, "source", SOURCE_ID))), int(getattr(value, "content_length", content_length) or 0), str(getattr(value, "output_sha256", output_sha256) or output_sha256), max(0, int(getattr(value, "reasoning_length", 0) or 0))


def _invoke_model(
    model: Any,
    request: Mapping[str, Any],
    *,
    system_prompt: str = SYSTEM_PROMPT,
) -> Any:
    complete = getattr(model, "complete", None)
    if complete is None and callable(model):
        return model(system_prompt, request, max_output_tokens=MAX_OUTPUT_TOKENS)
    if complete is None:
        raise CompactStageADevelopmentError("unsupported_model_interface")
    # This is one invocation.  Signature inspection avoids catching a provider
    # TypeError and accidentally retrying it as a different protocol.
    try:
        signature = inspect.signature(complete)
        parameters = signature.parameters
        names = list(parameters)
    except (TypeError, ValueError):
        parameters = {}
        names = []
    kwargs: Dict[str, Any] = {"max_output_tokens": MAX_OUTPUT_TOKENS}
    # An injected OpenAI-compatible adapter may expose the provider-specific
    # extension while ordinary fakes intentionally do not.  Pass the compact
    # Stage-A contract only when the signature can receive it; this keeps the
    # runner provider-agnostic without silently omitting thinking disablement
    # for the real adapter.
    accepts_extra_body = "extra_body" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    if accepts_extra_body:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    if names and names[0] in {"stage", "phase"}:
        return complete("A", system_prompt, request, **kwargs)
    return complete(system_prompt, request, **kwargs)


def _provider_error(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if code:
        safe = _safe_error_code(code)
        if safe != "unknown_error":
            return safe
        # Do not leak an arbitrary exception/code string into artifacts and
        # do not leave a wrapped exception indistinguishable from a protocol
        # validation failure.
        return "model_call_failed"
    name = type(exc).__name__.casefold()
    if "timeout" in name:
        return "model_call_failed"
    if "json" in name:
        return "provider_invalid_json"
    return "model_call_failed"


def _output_promotes_context_alias(value: Any, request: Mapping[str, Any]) -> bool:
    """Return whether a response promotes a known non-primary message alias."""

    candidate = value
    if isinstance(value, str):
        try:
            candidate = strict_json_loads(value)
        except Exception:
            return False
    if not isinstance(candidate, Mapping):
        return False
    topics = candidate.get("topics")
    if not isinstance(topics, list):
        return False
    message_aliases = {
        str(row.get("i"))
        for row in request.get("h", ())
        if isinstance(row, Mapping) and row.get("k") == "m"
    }
    primary_aliases = {
        str(row.get("i"))
        for row in request.get("h", ())
        if isinstance(row, Mapping) and row.get("k") == "m" and row.get("r") == "p"
    }
    for topic in topics:
        if not isinstance(topic, Mapping):
            continue
        primary = topic.get("primary_message_ids")
        if isinstance(primary, list) and any(
            str(alias) in message_aliases and str(alias) not in primary_aliases
            for alias in primary
        ):
            return True
    return False


def _validate_provider_output(value: Any, request: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate output and give source-primary promotions a stable code."""

    try:
        if isinstance(value, str):
            return parse_compact_stage_a_output(value, request)
        if isinstance(value, Mapping):
            return validate_compact_stage_a_output(dict(value), request)
        raise CompactStageADevelopmentError("provider_response_shape")
    except CompactStageAProtocolV3Error as exc:
        if exc.code == "output_primary_item" and _output_promotes_context_alias(value, request):
            raise CompactStageADevelopmentError(
                "primary_alias_not_source_primary",
                context_telemetry=getattr(exc, "context_telemetry", None),
            ) from exc
        if getattr(exc, "context_telemetry", None):
            raise CompactStageADevelopmentError(
                exc.code,
                context_telemetry=exc.context_telemetry,
            ) from exc
        raise


def _run_one(
    model: Any,
    page: Mapping[str, Any],
    request: Mapping[str, Any],
    categories: Tuple[str, ...],
    ledger: CallAuthorizationLedger,
    *,
    input_sha256: str,
    settings_sha256: str,
    scope: Mapping[str, str],
    cache: Optional[MutableMapping[str, Mapping[str, Any]]],
    system_prompt: str = SYSTEM_PROMPT,
    guard_id: Optional[str] = None,
    artifact_namespace: str = ARTIFACT_NAMESPACE,
) -> PageRun:
    request_hash = stable_hash(request)
    page_id = str(page.get("page_id", ""))
    root_id = str(page.get("root_id", ""))
    cue_codes = tuple(categories)
    model_id = str(getattr(model, "model_id", MODEL_ID))
    source_id = str(getattr(model, "source", SOURCE_ID))
    if cache is not None and request_hash in cache:
        try:
            compact = parse_compact_stage_a_output(str(cache[request_hash]), request) if isinstance(cache[request_hash], str) else cache[request_hash]
            resolved = resolve_compact_output(compact, request)
        except Exception:
            cache.pop(request_hash, None)
        else:
            return PageRun(page, request, request_hash, categories, "complete", compact, resolved, None, True, False, 0, 0, 0.0, model_id, "cache", cue_codes)
    stats = measure_wire_size(request)
    reservation = None
    started = time.perf_counter()
    try:
        reservation = ledger.reserve(
            request_sha256=request_hash,
            unit_ref=page_id,
            attempt=0,
            provider=PROVIDER_ID,
            model=model_id,
            protocol=PROTOCOL_VERSION,
            settings_sha256=settings_sha256,
            scope=scope,
            input_sha256=input_sha256,
            artifact_namespace=artifact_namespace,
            input_tokens_estimate=stats.http_token_proxy,
        )
    except CallBudgetExceeded:
        return PageRun(page, request, request_hash, categories, "pending", None, None, "authorization_call_budget_exhausted", False, False, 0, 0, 0.0, model_id, source_id, cue_codes)
    except ReservationRejected as exc:
        return PageRun(page, request, request_hash, categories, "pending", None, None, _safe_error_code(getattr(exc, "code", "input_token_limit_exceeded")), False, False, 0, 0, 0.0, model_id, source_id, cue_codes)
    except AuthorizationBindingMismatch:
        return PageRun(page, request, request_hash, categories, "pending", None, None, "authorization_binding_mismatch", False, False, 0, 0, 0.0, model_id, source_id, cue_codes)
    provider_call = True
    input_tokens = stats.http_token_proxy
    output_tokens = 0
    latency_ms = 0.0
    content_length = 0
    output_sha256 = ""
    reasoning_length = 0
    finish_reason = "not_run"
    context_error_telemetry: Mapping[str, Any] = {}
    try:
        ledger.mark_started(reservation)
        value = _invoke_model(model, request, system_prompt=system_prompt)
        try:
            (
                content,
                reported_in,
                reported_out,
                reported_latency,
                finish_reason,
                response_model,
                response_source,
                content_length,
                output_sha256,
                reasoning_length,
            ) = _response_parts(value, model)
        except CompactStageADevelopmentError:
            raise
        except (TypeError, ValueError, OverflowError) as exc:
            # Malformed synthetic/provider metadata is a response-shape
            # failure, not an arbitrary model-call exception.  Preserve the
            # fail-closed behavior while giving the body-free ledger a stable
            # category.
            raise CompactStageADevelopmentError("provider_response_metadata") from exc
        input_tokens = reported_in or input_tokens
        output_tokens = reported_out
        latency_ms = reported_latency or max(0.0, (time.perf_counter() - started) * 1000.0)
        if input_tokens > MAX_INPUT_TOKEN_PROXY:
            raise CompactStageADevelopmentError("input_token_limit_exceeded")
        if output_tokens > MAX_OUTPUT_TOKENS or finish_reason not in {"", "stop"}:
            raise CompactStageADevelopmentError("output_token_limit_exceeded")
        if guard_id:
            compact = validate_guard_validation_output(content, request, guard_id=guard_id)
        else:
            # Mapping payloads are synthetic-only conveniences; provider text
            # is still never persisted by this runner.
            compact = _validate_provider_output(content, request)
        resolved = resolve_compact_output(compact, request)
        if not output_tokens:
            output_tokens = measure_output_size(compact)["token_proxy"]
        if cache is not None:
            cache[request_hash] = deepcopy(compact)
        ledger.mark_complete(reservation, input_tokens=input_tokens, output_tokens=output_tokens, latency_ms=latency_ms)
        return PageRun(page, request, request_hash, categories, "complete", compact, resolved, None, False, provider_call, input_tokens, output_tokens, latency_ms, response_model, response_source, cue_codes, content_length, output_sha256, reasoning_length, finish_reason)
    except Exception as exc:
        if isinstance(getattr(exc, "context_telemetry", None), Mapping):
            context_error_telemetry = dict(getattr(exc, "context_telemetry"))
            if isinstance(page, MutableMapping):
                page["_context_error_telemetry"] = dict(context_error_telemetry)
        raw_code = getattr(exc, "code", None)
        code = _safe_error_code(raw_code) if raw_code else _provider_error(exc)
        # ``unknown_error`` is reserved for a direct safe-code projection;
        # an exception crossing the runner boundary must remain actionable as
        # a stable exception category.  Known protocol validation codes never
        # take this branch because they are in KNOWN_VALIDATION_ERROR_CODES.
        if code == "unknown_error":
            code = _provider_error(exc)
        input_tokens = max(0, int(getattr(exc, "input_tokens", input_tokens) or input_tokens))
        output_tokens = max(0, int(getattr(exc, "output_tokens", output_tokens) or output_tokens))
        latency_ms = max(0.0, float(getattr(exc, "latency_ms", latency_ms) or latency_ms))
        content_length = max(0, int(getattr(exc, "content_length", content_length) or content_length))
        output_sha256 = str(getattr(exc, "output_sha256", output_sha256) or output_sha256)
        reasoning_length = max(0, int(getattr(exc, "reasoning_length", reasoning_length) or reasoning_length))
        finish_reason = str(getattr(exc, "finish_reason", finish_reason) or finish_reason)[:80]
        latency_ms = latency_ms or max(0.0, (time.perf_counter() - started) * 1000.0)
        try:
            ledger.mark_failed(reservation, error_code=code, input_tokens=input_tokens, output_tokens=output_tokens, latency_ms=latency_ms)
        except Exception:
            code = "authorization_binding_mismatch"
        return PageRun(
            page, request, request_hash, categories, "pending", None, None, code,
            False, provider_call, input_tokens, output_tokens, latency_ms, model_id,
            source_id, cue_codes, content_length, output_sha256, reasoning_length,
            finish_reason, context_error_telemetry=context_error_telemetry,
        )


def _safe_public_page(page: Mapping[str, Any], categories: Sequence[str], rank: int, status: str, request: Mapping[str, Any], request_hash: str) -> Dict[str, Any]:
    scope = _scope(page.get("scope"))
    source_binding = _source_primary_binding(page, {})
    eligible_flag = page.get("_source_primary_eligible")
    source_primary_eligible = (
        bool(eligible_flag)
        if isinstance(eligible_flag, bool)
        else bool(
            source_binding["provided"]
            and any(
                isinstance(row, Mapping) and row.get("k") == "m" and row.get("r") == "p"
                for row in request.get("h", ())
            )
        )
    )
    role_projection = page.get("_role_projection_telemetry")
    if not isinstance(role_projection, Mapping):
        role_projection = _role_projection_telemetry(page, request)
    role_projection = _body_free(dict(role_projection))
    context_selection = page.get("_context_selection_telemetry")
    context_selection = _body_free(dict(context_selection)) if isinstance(context_selection, Mapping) else {}
    component_telemetry = page.get("_candidate_component_telemetry")
    component_telemetry = _body_free(dict(component_telemetry)) if isinstance(component_telemetry, Mapping) else {}
    recovery = context_recovery_snapshot(page)
    output = {
        "selection_rank": int(page.get("_selection_rank", rank)),
        "page_id": str(page.get("page_id", "")),
        "root_id": str(page.get("root_id", "")),
        "source_packet_id": str(page.get("source_packet_id", page.get("root_id", ""))),
        "scope": scope,
        "categories": list(categories),
        "status": status,
        "message_count": sum(1 for row in request.get("h", ()) if row.get("k") == "m"),
        "candidate_count": sum(1 for row in request.get("h", ()) if row.get("k") == "c"),
        "request_sha256": request_hash,
        "source_primary_eligible": source_primary_eligible,
        "source_primary_sha256": str(page.get("_source_primary_sha256") or source_binding["sha256"]),
        "role_projection": role_projection,
        "context_selection": context_selection,
        "context_kept": int(context_selection.get("context_kept", context_selection.get("kept", 0)) or 0),
        "context_deferred": int(context_selection.get("context_deferred", context_selection.get("deferred", 0)) or 0),
        "context_coverage": float(context_selection.get("context_coverage", 1.0) or 0.0),
        "proxy": int(context_selection.get("proxy", context_selection.get("http_token_proxy", 0)) or 0),
        "calibrated_proxy": int(context_selection.get("calibrated_proxy", context_selection.get("calibrated_input_token_proxy", 0)) or 0),
        "token_calibration_version": str(context_selection.get("calibration_version", TOPIC_GUIDED_TOKEN_CALIBRATION_VERSION)),
        "recovery_snapshot": {
            "version": recovery.get("version", TOPIC_GUIDED_CONTEXT_SELECTION_VERSION),
            "complete_context_count": len(recovery.get("complete_context_handles", ())),
            "kept_context_count": len(recovery.get("kept_context_handles", ())),
            "deferred_context_count": len(recovery.get("deferred_context_handles", ())),
            "source_ref_count": int(recovery.get("source_ref_count", 0) or 0),
            "snapshot_sha256": str(recovery.get("snapshot_sha256", "")),
            "body_free": True,
        },
        "candidate_components": {
            "component_count": int(component_telemetry.get("component_count", 0) or 0),
            "split_evidence_count": int(component_telemetry.get("split_evidence_count", 0) or 0),
            "merge_prior": str(component_telemetry.get("merge_prior", "")),
            "candidate_only": True,
            "model_decision_required": True,
            "final_topic_assignment": False,
            "under_uncertainty": "prefer_merge",
            "no_one_message_per_topic": True,
            "body_free": True,
        },
        "source_role_counts": dict(role_projection.get("source_role_counts", {})),
        "projected_primary_count": int(role_projection.get("projected_primary_count", 0) or 0),
        "projected_context_count": int(role_projection.get("projected_context_count", 0) or 0),
        "substantive_adjacent_promoted_count": int(role_projection.get("substantive_adjacent_promoted_count", 0) or 0),
        "role_conflict": bool(role_projection.get("role_conflict", False)),
        "topic_limit_cause": str(role_projection.get("topic_limit_cause", "")),
        "telemetry_codes": list(role_projection.get("telemetry_codes", ())),
        "unbound_substantive_candidate_not_promoted_count": int(
            role_projection.get("unbound_substantive_candidate_not_promoted_count", 0) or 0
        ),
        "media_mispromotion_count": int(role_projection.get("media_mispromotion_count", 0) or 0),
        "empty_authority_mispromotion_count": int(role_projection.get("empty_authority_mispromotion_count", 0) or 0),
        "placeholder_mispromotion_count": int(role_projection.get("placeholder_mispromotion_count", 0) or 0),
        "reaction_mispromotion_count": int(role_projection.get("reaction_mispromotion_count", 0) or 0),
        "system_or_event_mispromotion_count": int(role_projection.get("system_or_event_mispromotion_count", 0) or 0),
        "unbound_primary_promotion_count": int(role_projection.get("unbound_primary_promotion_count", 0) or 0),
        "barrier_mispromotion_zero": bool(role_projection.get("barrier_mispromotion_zero", True)),
        "page_hash": str(page.get("page_hash", "")),
        "cue_preserved": True,
    }
    for key, output_key in (
        ("_source_handle", "source_handle"),
        ("_scope_handle", "scope_handle"),
    ):
        if page.get(key) not in (None, ""):
            output[output_key] = str(page[key])
    for key in ("_candidate_only", "_not_canonical", "_semantic_decision_pending"):
        if key in page:
            output[key[1:]] = bool(page[key])
    if page.get("_guard_id") not in (None, ""):
        output["guard_id"] = str(page["_guard_id"])
    if page.get("_guard_expected_error_code") not in (None, ""):
        output["guard_expected_error_code"] = str(page["_guard_expected_error_code"])
    return output


def _aggregate_role_projection(runs: Sequence[PageRun]) -> Dict[str, Any]:
    """Aggregate per-page role telemetry without retaining request cues."""

    source_counts: Dict[str, int] = {
        "primary": 0,
        "context": 0,
        "authority": 0,
        "candidate": 0,
        "unbound": 0,
    }
    cause_counts: Dict[str, int] = {}
    conflict_reasons: set[str] = set()
    source_primary_count = 0
    source_context_count = 0
    projected_primary_count = 0
    projected_context_count = 0
    promoted_count = 0
    conflict_count = 0
    telemetry_code_counts: Dict[str, int] = {}
    mispromotion_totals: Dict[str, int] = {
        "media": 0,
        "empty_authority": 0,
        "placeholder": 0,
        "reaction": 0,
        "system_or_event": 0,
        "unbound": 0,
    }
    for run in runs:
        telemetry = run.role_projection if isinstance(run.role_projection, Mapping) else {}
        nested_counts = telemetry.get("source_role_counts")
        if isinstance(nested_counts, Mapping):
            for key in source_counts:
                try:
                    source_counts[key] += max(0, int(nested_counts.get(key, 0) or 0))
                except (TypeError, ValueError, OverflowError):
                    continue
        for key, target in (
            ("source_primary_count", "source_primary_count"),
            ("source_context_count", "source_context_count"),
            ("projected_primary_count", "projected_primary_count"),
            ("projected_context_count", "projected_context_count"),
            ("substantive_adjacent_promoted_count", "promoted_count"),
            ("role_conflict_count", "conflict_count"),
        ):
            try:
                value = max(0, int(telemetry.get(key, 0) or 0))
            except (TypeError, ValueError, OverflowError):
                value = 0
            if target == "source_primary_count":
                source_primary_count += value
            elif target == "source_context_count":
                source_context_count += value
            elif target == "projected_primary_count":
                projected_primary_count += value
            elif target == "projected_context_count":
                projected_context_count += value
            elif target == "promoted_count":
                promoted_count += value
            else:
                conflict_count += value
        cause = str(telemetry.get("topic_limit_cause") or "unknown")
        cause_counts[cause] = cause_counts.get(cause, 0) + 1
        reasons = telemetry.get("role_conflict_reasons")
        if isinstance(reasons, (list, tuple, set, frozenset)):
            conflict_reasons.update(str(item) for item in reasons if item)
        codes = telemetry.get("telemetry_codes")
        if isinstance(codes, (list, tuple, set, frozenset)):
            for code in codes:
                code_text = str(code)
                if code_text:
                    telemetry_code_counts[code_text] = telemetry_code_counts.get(code_text, 0) + 1
        barrier_counts = telemetry.get("barrier_mispromotion_counts")
        if isinstance(barrier_counts, Mapping):
            for name in mispromotion_totals:
                try:
                    mispromotion_totals[name] += max(0, int(barrier_counts.get(name, 0) or 0))
                except (TypeError, ValueError, OverflowError):
                    continue
    return {
        "rows": len(runs),
        "source_role_counts": source_counts,
        "source_primary_count": source_primary_count,
        "source_context_count": source_context_count,
        "source_adjacent_count": source_counts["context"],
        "source_authority_count": source_counts["authority"],
        "source_candidate_count": source_counts["candidate"],
        "source_nonprimary_count": source_context_count,
        "projected_primary_count": projected_primary_count,
        "projected_context_count": projected_context_count,
        "source_role_primary_count": source_primary_count,
        "source_role_context_count": source_context_count,
        "source_role_vs_projected": {
            "source_primary": source_primary_count,
            "source_context": source_context_count,
            "projected_primary": projected_primary_count,
            "projected_context": projected_context_count,
        },
        "substantive_adjacent_promoted_count": promoted_count,
        "role_conflict_count": conflict_count,
        "role_conflict": bool(conflict_count),
        "role_conflict_reasons": sorted(conflict_reasons),
        "topic_limit_cause_counts": dict(sorted(cause_counts.items())),
        "topic_limit": projected_primary_count,
        "topic_limit_cause": (
            next(iter(cause_counts)) if len(cause_counts) == 1 else "mixed"
        ),
        "telemetry_codes": sorted(telemetry_code_counts),
        "telemetry_code_counts": dict(sorted(telemetry_code_counts.items())),
        "unbound_substantive_candidate_not_promoted_count": int(
            telemetry_code_counts.get("unbound_substantive_candidate_not_promoted", 0)
        ),
        "media_mispromotion_count": int(mispromotion_totals["media"]),
        "empty_authority_mispromotion_count": int(mispromotion_totals["empty_authority"]),
        "placeholder_mispromotion_count": int(mispromotion_totals["placeholder"]),
        "reaction_mispromotion_count": int(mispromotion_totals["reaction"]),
        "system_or_event_mispromotion_count": int(mispromotion_totals["system_or_event"]),
        "unbound_primary_promotion_count": int(mispromotion_totals["unbound"]),
        "barrier_mispromotion_counts": dict(mispromotion_totals),
        "barrier_mispromotion_zero": not any(mispromotion_totals.values()),
        "body_free": True,
    }


def _aggregate_context_selection(runs: Sequence[PageRun]) -> Dict[str, Any]:
    """Aggregate deterministic context-view selection telemetry."""

    totals = {
        "context_total": 0,
        "context_kept": 0,
        "context_deferred": 0,
        "deduplicated_context_count": 0,
        "rows": len(runs),
    }
    kept_reason_counts: Dict[str, int] = {}
    deferred_reason_counts: Dict[str, int] = {}
    proxy_values: List[int] = []
    calibrated_values: List[int] = []
    for run in runs:
        selection = run.context_selection if isinstance(run.context_selection, Mapping) else {}
        for key in tuple(totals):
            if key == "rows":
                continue
            try:
                totals[key] += max(0, int(selection.get(key, 0) or 0))
            except (TypeError, ValueError, OverflowError):
                continue
        for source, target in (("kept_reason_counts", kept_reason_counts), ("deferred_reason_counts", deferred_reason_counts)):
            value = selection.get(source)
            if isinstance(value, Mapping):
                for name, count in value.items():
                    try:
                        target[str(name)] = target.get(str(name), 0) + max(0, int(count or 0))
                    except (TypeError, ValueError, OverflowError):
                        continue
        try:
            proxy = int(selection.get("proxy", selection.get("http_token_proxy", 0)) or 0)
            if proxy:
                proxy_values.append(proxy)
        except (TypeError, ValueError, OverflowError):
            pass
        try:
            calibrated = int(selection.get("calibrated_proxy", selection.get("calibrated_input_token_proxy", 0)) or 0)
            if calibrated:
                calibrated_values.append(calibrated)
        except (TypeError, ValueError, OverflowError):
            pass
    total = totals["context_total"]
    kept = totals["context_kept"]
    totals.update(
        {
            "kept_reason_counts": dict(sorted(kept_reason_counts.items())),
            "deferred_reason_counts": dict(sorted(deferred_reason_counts.items())),
            "context_coverage": (float(kept) / float(total)) if total else 1.0,
            "proxy_min": min(proxy_values) if proxy_values else 0,
            "proxy_max": max(proxy_values) if proxy_values else 0,
            "calibrated_proxy_min": min(calibrated_values) if calibrated_values else 0,
            "calibrated_proxy_max": max(calibrated_values) if calibrated_values else 0,
            "calibration_version": TOPIC_GUIDED_TOKEN_CALIBRATION_VERSION,
            "selection_version": TOPIC_GUIDED_CONTEXT_SELECTION_VERSION,
            "primary_never_deferred": True,
            "context_role": "c",
            "body_free": True,
        }
    )
    return totals


def _write_artifacts(
    output_root: Path,
    *,
    authorization_id: str,
    authorization_contract: Optional[Mapping[str, Any]],
    input_label: str,
    input_binding_sha256: str,
    input_hashes: Mapping[str, str],
    manifest_input: Mapping[str, Any],
    selected: Sequence[Tuple[Mapping[str, Any], Tuple[str, ...]]],
    runs: Sequence[PageRun],
    missing_strata: Sequence[str],
    selection_plan: Optional[CompactStageASelectionPlan],
    ledger: CallAuthorizationLedger,
    health_reused: bool,
    provider_model: str,
    provider_source: str,
    lineage: Optional[Mapping[str, Any]] = None,
    input_artifact_version: Optional[str] = None,
    selection_report_override: Optional[Mapping[str, Any]] = None,
    artifact_version: str = ARTIFACT_VERSION,
    selected_page_limit: int = MAX_SELECTED_PAGES,
    provider_call_limit: int = MAX_PROVIDER_CALLS,
    per_page_provider_call_limit: int = PER_PAGE_PROVIDER_CALL_LIMIT,
    max_retries: int = MAX_RETRIES,
    sdk_calls: int = 0,
    guard_validation: bool = False,
    guarded_authorization: bool = False,
    authority_bound_authorization: bool = False,
    topic_guided_authorization: bool = False,
    guard_prompt_version: Optional[str] = None,
    guard_taxonomy_version: Optional[str] = None,
    guard_code_sha256: Optional[str] = None,
    artifact_namespace: str = ARTIFACT_NAMESPACE,
) -> Tuple[Dict[str, str], Dict[str, Any], str, bool]:
    provider_calls = sum(bool(run.provider_call) for run in runs)
    pending_count = sum(run.status != "complete" for run in runs)
    complete_count = len(runs) - pending_count
    errors = [
        {
            "phase": "development",
            "page_id": str(run.page.get("page_id", "")),
            "root_id": str(run.page.get("root_id", "")),
            "error_code": run.error_code,
            "error_category": run.error_category,
            "validation_categories": list(run.validation_categories),
            "context_error_telemetry": _body_free(dict(run.context_error_telemetry)),
            "context_error_counts": dict((run.context_error_telemetry or {}).get("error_counts", {}))
            if isinstance(run.context_error_telemetry, Mapping)
            else {},
            "status": run.status,
            "cue_preserved": True,
        }
        for run in runs
        if run.error_code
    ]
    if missing_strata:
        errors.append({"phase": "selection", "error_code": "selection_strata_missing", "missing_strata": list(missing_strata), "status": "diagnostic"})
    if selection_plan is not None and selection_plan.ambiguous_page_ids:
        errors.append({"phase": "selection", "error_code": "selection_strata_ambiguous", "ambiguous_page_count": len(selection_plan.ambiguous_page_ids), "status": "diagnostic"})
    if selection_plan is not None and selection_plan.scope_authorization_required:
        errors.append({"phase": "selection", "error_code": "multi_scope_authorization_required", "global_coverage_plan_scope_count": selection_plan.global_coverage_plan_scope_count, "status": "diagnostic"})
    selection_rows = [
        _safe_public_page(page, categories, index + 1, run.status, run.request, run.request_sha256)
        for index, ((page, categories), run) in enumerate(zip(selected, runs))
    ]
    decision_rows = [run.to_dict() for run in runs]
    diagnostic_rows = [run.diagnostic_dict() for run in runs]
    role_projection = _aggregate_role_projection(runs)
    context_selection = _aggregate_context_selection(runs)
    run_by_request = {run.request_sha256: run for run in runs}
    ledger_rows = []
    for row in ledger.records():
        enriched = dict(row)
        run = run_by_request.get(str(row.get("request_sha256", "")))
        if run is not None:
            enriched["source_primary_eligible"] = bool(run.source_primary_eligible)
            enriched["source_primary_sha256"] = str(run.source_primary_sha256)
        else:
            enriched["source_primary_eligible"] = False
            enriched["source_primary_sha256"] = stable_hash({"source_primary": []})
        ledger_rows.append(enriched)
    selection_report = dict(selection_report_override) if selection_report_override is not None else selection_plan.to_dict(selected_page_limit=selected_page_limit) if selection_plan is not None else {
        "selected_page_count": len(selected),
        "selected_page_limit": selected_page_limit,
        "missing_strata": list(missing_strata),
        "available_strata": [category for category in CATEGORY_NAMES if category not in missing_strata],
        "selection_rule": "rare_strata_first_then_scope_then_budget",
        "page_categories_exclusive": True,
        "body_free": True,
    }
    source_input_artifact_version = str(
        input_artifact_version
        or manifest_input.get("artifact_version")
        or INPUT_ARTIFACT_VERSION
    )
    safe_lineage = _body_free(dict(lineage or {}))
    status = "complete" if runs and not pending_count and len(runs) == int(selected_page_limit) and not missing_strata and not (selection_plan and selection_plan.scope_authorization_required) else "partial"
    success = status == "complete"
    aggregate: Dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "artifact_version": artifact_version,
        "runner_schema_version": RUNNER_SCHEMA_VERSION,
        "error_taxonomy_version": ERROR_TAXONOMY_VERSION,
        "authorization_id": authorization_id,
        "artifact_namespace": artifact_namespace,
        "max_total_calls": int(provider_call_limit),
        "status": status,
        "success": success,
        "input_artifact_version": source_input_artifact_version,
        "input_directory_name": Path(input_label).name,
        "input_binding_sha256": input_binding_sha256,
        "input_hashes": dict(input_hashes),
        "development_input_read": bool(selected),
        "frozen_read": False,
        "gold_loaded": False,
        "production_state_written": False,
        "stage_a_development": bool(selected),
        "stage_b_pilot": False,
        "stage_c_pilot": False,
        "provider": {
            "provider": PROVIDER_ID,
            "source": provider_source,
            "model": provider_model,
            "protocol": PROTOCOL_VERSION,
            "response_format_mode": RESPONSE_FORMAT_MODE,
            "response_format_sent": False,
            "thinking_disabled": THINKING_DISABLED,
            "calls": provider_calls,
            "call_limit": provider_call_limit,
            "max_total_calls": int(provider_call_limit),
            "retry_count": max_retries,
            "sdk_calls": int(sdk_calls),
        },
        "selection": selection_report,
        "decisions": {
            "selected": len(runs),
            "complete": complete_count,
            "pending": pending_count,
            "complete_only_outputs": True,
            "pending_cue_preserved": all(run.cue_codes and run.status == "pending" for run in runs if run.status == "pending") if pending_count else True,
            "role_projection": role_projection,
            "context_selection": context_selection,
        },
        "role_projection": role_projection,
        "projection_telemetry": role_projection,
        "context_selection": context_selection,
        "context_telemetry": context_selection,
        "response_diagnostics": {
            "taxonomy_version": ERROR_TAXONOMY_VERSION,
            "rows": len(diagnostic_rows),
            "content_length_total": sum(int(row["content_length"]) for row in diagnostic_rows),
            "reasoning_length_total": sum(int(row["reasoning_length"]) for row in diagnostic_rows),
            "input_tokens_total": sum(int(row["input_tokens"]) for row in diagnostic_rows),
            "output_tokens_total": sum(int(row["output_tokens"]) for row in diagnostic_rows),
            "finish_reason_counts": {
                reason: sum(1 for row in diagnostic_rows if row["finish_reason"] == reason)
                for reason in sorted({str(row["finish_reason"]) for row in diagnostic_rows})
            },
            "status_counts": {
                status_value: sum(1 for row in diagnostic_rows if row["status"] == status_value)
                for status_value in sorted({str(row["status"]) for row in diagnostic_rows})
            },
            "error_code_counts": {
                code: sum(1 for row in diagnostic_rows if row.get("error_code") == code)
                for code in sorted({str(row.get("error_code")) for row in diagnostic_rows if row.get("error_code")})
            },
            "validation_category_counts": {
                category: sum(
                    1
                    for row in diagnostic_rows
                    if category in (row.get("validation_categories") or ())
                )
                for category in sorted(
                    {
                        str(category)
                        for row in diagnostic_rows
                        for category in (row.get("validation_categories") or ())
                    }
                )
            },
            "telemetry_codes": list(role_projection.get("telemetry_codes", ())),
            "telemetry_code_counts": dict(role_projection.get("telemetry_code_counts", {})),
            "unbound_substantive_candidate_not_promoted_count": int(
                role_projection.get("unbound_substantive_candidate_not_promoted_count", 0) or 0
            ),
            "unknown_error_count": sum(1 for row in diagnostic_rows if row.get("error_code") == "unknown_error"),
            "role_projection": role_projection,
            "context_selection": context_selection,
            "context_error_counts": {
                name: sum(
                    int((row.get("context_error_counts") or {}).get(name, 0) or 0)
                    for row in diagnostic_rows
                    if isinstance(row.get("context_error_counts"), Mapping)
                )
                for name in CONTEXT_ERROR_TELEMETRY_KEYS
            },
            "body_free": True,
        },
        "cost": {
            "provider_calls": provider_calls,
            "input_tokens": sum(run.input_tokens for run in runs),
            "output_tokens": sum(run.output_tokens for run in runs),
            "latency_ms": round(sum(run.latency_ms for run in runs), 3),
            "max_provider_calls": provider_call_limit,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "max_input_token_proxy": MAX_INPUT_TOKEN_PROXY,
            "retry_count": max_retries,
            "cache_hits": sum(bool(run.cache_hit) for run in runs),
            "sdk_calls": int(sdk_calls),
        },
        "health": {"reused": bool(health_reused), "new_calls": 0, "health_required": True},
        "authorization_ledger": ledger.snapshot(),
        "errors": {"count": len(errors), "codes": sorted({str(row.get("error_code")) for row in errors if row.get("error_code")})},
        "input_manifest_status": str(manifest_input.get("status", "unknown")),
    }
    if guard_validation:
        aggregate.update(
            {
                "guard_validation": True,
                "guard_prompt_version": str(guard_prompt_version or ""),
                "guard_taxonomy_version": str(guard_taxonomy_version or ""),
                "guard_code_sha256": str(guard_code_sha256 or ""),
                "guard_subset_sha256": str((safe_lineage or {}).get("subset_sha256", "")),
                "sdk_calls": int(sdk_calls),
            }
        )
    if guarded_authorization:
        aggregate.update(
            {
                "guarded_authorization": True,
                "guard_prompt_version": str(guard_prompt_version or ""),
                "guard_taxonomy_version": str(guard_taxonomy_version or ""),
                "guard_code_sha256": str(guard_code_sha256 or ""),
                "sdk_calls": int(sdk_calls),
                "supplement_calls": 0,
                "supplement_pages": 0,
                "no_supplement": True,
            }
        )
    if authority_bound_authorization:
        aggregate.update(
            {
                "authority_bound_authorization": True,
                "authority_bound": True,
                "authority_projection_hashes": {
                    str(key): str(value)
                    for key, value in (safe_lineage.get("authority_projection_hashes", {}) or {}).items()
                },
                "authority_projection_audit_sha256": str(
                    (safe_lineage.get("authority_projection_hashes", {}) or {}).get(
                        "authority_projection_audit_sha256", ""
                    )
                ),
                "authority_projection_code_sha256": str(
                    (safe_lineage.get("authority_projection_hashes", {}) or {}).get(
                        "authority_projection_code_sha256", ""
                    )
                ),
                "authority_projection_selection_sha256": str(
                    (safe_lineage.get("authority_projection_hashes", {}) or {}).get(
                        "authority_projection_selection_sha256", ""
                    )
                ),
                "authority_projection_context_sha256": str(
                    (safe_lineage.get("authority_projection_hashes", {}) or {}).get(
                        "authority_projection_context_sha256", ""
                    )
                ),
                "authority_projection_binding_sha256": str(safe_lineage.get("authority_projection_binding_sha256", "")),
                "authority_projection_page_refs": list(safe_lineage.get("authority_projection_page_refs", ())),
                "authority_projection_scope_ref": str(safe_lineage.get("authority_projection_scope_ref", "")),
                "authority_bound_canonical_mapping_version": str(
                    safe_lineage.get("authority_bound_canonical_mapping_version", "")
                ),
                "authority_bound_canonical_mapping_sha256": str(
                    safe_lineage.get("authority_bound_canonical_mapping_sha256", "")
                ),
                "authority_bound_canonical_mapping": [
                    dict(row)
                    for row in (safe_lineage.get("authority_bound_canonical_mapping", ()) or ())
                    if isinstance(row, Mapping)
                ],
                "max_total_calls": AUTHORITY_BOUND_MAX_TOTAL_CALLS,
                "selected_page_limit": AUTHORITY_BOUND_MAX_SELECTED_PAGES,
                "per_page_provider_call_limit": AUTHORITY_BOUND_PER_PAGE_PROVIDER_CALL_LIMIT,
                "retry_count": AUTHORITY_BOUND_MAX_RETRIES,
                "supplement_calls": AUTHORITY_BOUND_SUPPLEMENT_CALLS,
                "supplement_pages": AUTHORITY_BOUND_SUPPLEMENT_PAGES,
                "health_calls": AUTHORITY_BOUND_HEALTH_CALLS,
                "sdk_calls": AUTHORITY_BOUND_SDK_CALLS,
                "no_supplement": True,
                "response_format_mode": RESPONSE_FORMAT_MODE,
                "response_format_sent": False,
                "thinking_disabled": THINKING_DISABLED,
                "health": {"reused": False, "new_calls": 0, "health_required": False},
                "stage_b_authorized": False,
                "stage_c_authorized": False,
                "production_authorized": False,
                "frozen_read": False,
                "gold_loaded": False,
            }
        )
    if topic_guided_authorization:
        aggregate.update(
            {
                "topic_guided_authorization": True,
                "topic_guided": True,
                "authority_bound_projection": True,
                "topic_guided_prompt_version": TOPIC_GUIDED_PROMPT_VERSION,
                "topic_guided_prompt_sha256": str((safe_lineage or {}).get("topic_guided_prompt_sha256", "")),
                "topic_guided_guidance_version": TOPIC_GUIDED_GUIDANCE_VERSION,
                "topic_guided_guidance_sha256": str((safe_lineage or {}).get("topic_guided_guidance_sha256", "")),
                "topic_guided_grouping_hints_version": TOPIC_GUIDED_GROUPING_HINTS_VERSION,
                "topic_guided_grouping_hints_policy_sha256": str((safe_lineage or {}).get("topic_guided_grouping_hints_policy_sha256", "")),
                "topic_guided_g_hint_binding_sha256": str((safe_lineage or {}).get("topic_guided_g_hint_binding_sha256", "")),
                "topic_guided_protocol_source_sha256": str((safe_lineage or {}).get("topic_guided_protocol_source_sha256", "")),
                "topic_guided_review_sha256": str((safe_lineage or {}).get("topic_guided_review_sha256", "")),
                "topic_guided_review_hashes": dict((safe_lineage or {}).get("topic_guided_review_hashes", {}) or {}),
                "max_total_calls": TOPIC_GUIDED_MAX_TOTAL_CALLS,
                "max_provider_calls": TOPIC_GUIDED_MAX_PROVIDER_CALLS,
                "selected_page_limit": TOPIC_GUIDED_MAX_SELECTED_PAGES,
                "per_page_provider_call_limit": TOPIC_GUIDED_PER_PAGE_PROVIDER_CALL_LIMIT,
                "retry_count": TOPIC_GUIDED_MAX_RETRIES,
                "supplement_calls": TOPIC_GUIDED_SUPPLEMENT_CALLS,
                "supplement_pages": TOPIC_GUIDED_SUPPLEMENT_PAGES,
                "health_calls": TOPIC_GUIDED_HEALTH_CALLS,
                "sdk_calls": TOPIC_GUIDED_SDK_CALLS,
                "no_supplement": True,
                "response_format_mode": RESPONSE_FORMAT_MODE,
                "response_format_sent": False,
                "thinking_disabled": THINKING_DISABLED,
                "health": {"reused": False, "new_calls": 0, "health_required": False},
                "stage_b_authorized": False,
                "stage_c_authorized": False,
                "production_authorized": False,
                "frozen_read": False,
                "gold_loaded": False,
            }
        )
    if authorization_contract:
        aggregate["authorization_contract"] = _body_free(dict(authorization_contract))
    if safe_lineage:
        aggregate["lineage"] = safe_lineage
        for key in (
            "authorization_id",
            "adapter",
            "adapter_source",
            "adapter_sha256",
            "adapter_code_sha256",
            "code_sha256",
            "source_code_sha256",
            "selection_sha256",
            "context_input_sha256",
            "audit_summary_sha256",
            "human_audit_sha256",
            "candidate_only",
            "not_canonical",
            "semantic_decision_pending",
            "provider_primary_role",
            "settings_sha256",
            "model",
            "protocol",
            "scope_sha256",
            "max_provider_calls",
            "per_page_provider_call_limit",
            "max_retries",
            "audit_authorization_source",
            "stage_b_authorized",
            "stage_c_authorized",
            "production_authorized",
            "stratified_current",
            "subset_sha256",
            "guard_audit_summary_sha256",
            "guard_human_audit_sha256",
            "guards_replay_sha256",
            "guard_prompt_version",
            "guard_taxonomy_version",
            "guard_code_sha256",
            "guard_page_refs",
            "guard_ids",
            "guarded_authorization",
            "authority_bound_authorization",
            "authority_bound",
            "authority_projection_hashes",
            "authority_projection_binding_sha256",
            "authority_projection_page_refs",
            "authority_projection_scope_ref",
            "authority_bound_canonical_mapping_version",
            "authority_bound_canonical_mapping_sha256",
            "authority_bound_canonical_mapping",
            "authority_bound_code_sha256",
            "authority_bound_prompt_version",
            "authority_bound_prompt_sha256",
            "authority_bound_taxonomy_version",
            "topic_guided_authorization",
            "topic_guided",
            "authority_bound_projection",
            "topic_guided_code_sha256",
            "topic_guided_prompt_version",
            "topic_guided_prompt_sha256",
            "topic_guided_guidance_version",
            "topic_guided_guidance_sha256",
            "topic_guided_grouping_hints_version",
            "topic_guided_grouping_hints_policy_sha256",
            "topic_guided_g_hint_binding_sha256",
            "topic_guided_protocol_source_sha256",
            "topic_guided_taxonomy_version",
            "topic_guided_review_hashes",
            "topic_guided_review_sha256",
            "max_total_calls",
            "health_calls",
            "supplement_calls",
            "supplement_pages",
            "no_supplement",
        ):
            if key in safe_lineage:
                aggregate[key] = safe_lineage[key]
    cost = dict(aggregate["cost"])
    if topic_guided_authorization:
        cost.update(
            {
                "max_total_calls": TOPIC_GUIDED_MAX_TOTAL_CALLS,
                "max_provider_calls": TOPIC_GUIDED_MAX_PROVIDER_CALLS,
                "per_page_provider_call_limit": TOPIC_GUIDED_PER_PAGE_PROVIDER_CALL_LIMIT,
                "retry_count": TOPIC_GUIDED_MAX_RETRIES,
                "supplement_calls": TOPIC_GUIDED_SUPPLEMENT_CALLS,
                "supplement_pages": TOPIC_GUIDED_SUPPLEMENT_PAGES,
                "health_calls": TOPIC_GUIDED_HEALTH_CALLS,
                "sdk_calls": TOPIC_GUIDED_SDK_CALLS,
            }
        )
        aggregate["cost"] = cost
    elif authority_bound_authorization:
        cost.update(
            {
                "max_total_calls": AUTHORITY_BOUND_MAX_TOTAL_CALLS,
                "max_provider_calls": AUTHORITY_BOUND_MAX_PROVIDER_CALLS,
                "per_page_provider_call_limit": AUTHORITY_BOUND_PER_PAGE_PROVIDER_CALL_LIMIT,
                "retry_count": AUTHORITY_BOUND_MAX_RETRIES,
                "supplement_calls": AUTHORITY_BOUND_SUPPLEMENT_CALLS,
                "supplement_pages": AUTHORITY_BOUND_SUPPLEMENT_PAGES,
                "health_calls": AUTHORITY_BOUND_HEALTH_CALLS,
                "sdk_calls": AUTHORITY_BOUND_SDK_CALLS,
            }
        )
        aggregate["cost"] = cost
    manifest: Dict[str, Any] = {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "artifact_version": artifact_version,
        "input_artifact_version": source_input_artifact_version,
        "local_day": LOCAL_DAY,
        "split": "development",
        "status": status,
        "success": success,
        "authorization_id": authorization_id,
        "artifact_namespace": artifact_namespace,
        "input_directory_name": Path(input_label).name,
        "input_binding_sha256": input_binding_sha256,
        "input_hashes": dict(input_hashes),
        "development_input_read": bool(selected),
        "frozen_read": False,
        "gold_loaded": False,
        "production_state_written": False,
        "provider_called": provider_calls > 0,
        "provider_calls": provider_calls,
        "provider_call_limit": provider_call_limit,
        "retry_count": max_retries,
        "error_taxonomy_version": ERROR_TAXONOMY_VERSION,
        "per_page_provider_call_limit": per_page_provider_call_limit,
        "selected_page_count": len(selected),
        "selected_page_limit": selected_page_limit,
        "stage_a_development": bool(selected),
        "stage_b_pilot": False,
        "stage_c_pilot": False,
        "provider": {"provider": PROVIDER_ID, "source": provider_source, "model": provider_model, "protocol": PROTOCOL_VERSION, "response_format_mode": RESPONSE_FORMAT_MODE, "response_format_sent": False, "thinking_disabled": THINKING_DISABLED, "sdk_calls": int(sdk_calls)},
        "health_reused": bool(health_reused),
        "health_calls": 0,
        "missing_strata": list(missing_strata),
        "selection": selection_report,
        "role_projection": role_projection,
        "projection_telemetry": role_projection,
        "output_files": dict(OUTPUT_FILENAMES),
    }
    if guard_validation:
        manifest.update(
            {
                "guard_validation": True,
                "guard_prompt_version": str(guard_prompt_version or ""),
                "guard_taxonomy_version": str(guard_taxonomy_version or ""),
                "guard_code_sha256": str(guard_code_sha256 or ""),
                "guard_subset_sha256": str((safe_lineage or {}).get("subset_sha256", "")),
                "sdk_calls": int(sdk_calls),
            }
        )
    if guarded_authorization:
        manifest.update(
            {
                "guarded_authorization": True,
                "guard_prompt_version": str(guard_prompt_version or ""),
                "guard_taxonomy_version": str(guard_taxonomy_version or ""),
                "guard_code_sha256": str(guard_code_sha256 or ""),
                "sdk_calls": int(sdk_calls),
                "supplement_calls": 0,
                "supplement_pages": 0,
                "no_supplement": True,
            }
        )
    if authority_bound_authorization:
        manifest.update(
            {
                "authority_bound_authorization": True,
                "authority_bound": True,
                "authority_projection_hashes": {
                    str(key): str(value)
                    for key, value in (safe_lineage.get("authority_projection_hashes", {}) or {}).items()
                },
                "authority_projection_audit_sha256": str(
                    (safe_lineage.get("authority_projection_hashes", {}) or {}).get(
                        "authority_projection_audit_sha256", ""
                    )
                ),
                "authority_projection_code_sha256": str(
                    (safe_lineage.get("authority_projection_hashes", {}) or {}).get(
                        "authority_projection_code_sha256", ""
                    )
                ),
                "authority_projection_selection_sha256": str(
                    (safe_lineage.get("authority_projection_hashes", {}) or {}).get(
                        "authority_projection_selection_sha256", ""
                    )
                ),
                "authority_projection_context_sha256": str(
                    (safe_lineage.get("authority_projection_hashes", {}) or {}).get(
                        "authority_projection_context_sha256", ""
                    )
                ),
                "authority_projection_binding_sha256": str(safe_lineage.get("authority_projection_binding_sha256", "")),
                "authority_projection_page_refs": list(safe_lineage.get("authority_projection_page_refs", ())),
                "authority_projection_scope_ref": str(safe_lineage.get("authority_projection_scope_ref", "")),
                "authority_bound_canonical_mapping_version": str(
                    safe_lineage.get("authority_bound_canonical_mapping_version", "")
                ),
                "authority_bound_canonical_mapping_sha256": str(
                    safe_lineage.get("authority_bound_canonical_mapping_sha256", "")
                ),
                "authority_bound_canonical_mapping": [
                    dict(row)
                    for row in (safe_lineage.get("authority_bound_canonical_mapping", ()) or ())
                    if isinstance(row, Mapping)
                ],
                "max_total_calls": AUTHORITY_BOUND_MAX_TOTAL_CALLS,
                "provider_call_limit": AUTHORITY_BOUND_MAX_TOTAL_CALLS,
                "selected_page_limit": AUTHORITY_BOUND_MAX_SELECTED_PAGES,
                "per_page_provider_call_limit": AUTHORITY_BOUND_PER_PAGE_PROVIDER_CALL_LIMIT,
                "retry_count": AUTHORITY_BOUND_MAX_RETRIES,
                "supplement_calls": AUTHORITY_BOUND_SUPPLEMENT_CALLS,
                "supplement_pages": AUTHORITY_BOUND_SUPPLEMENT_PAGES,
                "health_calls": AUTHORITY_BOUND_HEALTH_CALLS,
                "sdk_calls": AUTHORITY_BOUND_SDK_CALLS,
                "no_supplement": True,
                "response_format_mode": RESPONSE_FORMAT_MODE,
                "response_format_sent": False,
                "thinking_disabled": THINKING_DISABLED,
                "health_required": False,
                "stage_b_authorized": False,
                "stage_c_authorized": False,
                "production_authorized": False,
                "frozen_read": False,
                "gold_loaded": False,
            }
        )
    if topic_guided_authorization:
        manifest.update(
            {
                "topic_guided_authorization": True,
                "topic_guided": True,
                "authority_bound_projection": True,
                "topic_guided_prompt_version": TOPIC_GUIDED_PROMPT_VERSION,
                "topic_guided_prompt_sha256": str((safe_lineage or {}).get("topic_guided_prompt_sha256", "")),
                "topic_guided_guidance_version": TOPIC_GUIDED_GUIDANCE_VERSION,
                "topic_guided_guidance_sha256": str((safe_lineage or {}).get("topic_guided_guidance_sha256", "")),
                "topic_guided_grouping_hints_version": TOPIC_GUIDED_GROUPING_HINTS_VERSION,
                "topic_guided_grouping_hints_policy_sha256": str((safe_lineage or {}).get("topic_guided_grouping_hints_policy_sha256", "")),
                "topic_guided_g_hint_binding_sha256": str((safe_lineage or {}).get("topic_guided_g_hint_binding_sha256", "")),
                "topic_guided_protocol_source_sha256": str((safe_lineage or {}).get("topic_guided_protocol_source_sha256", "")),
                "topic_guided_taxonomy_version": TOPIC_GUIDED_TAXONOMY_VERSION,
                "topic_guided_review_sha256": str((safe_lineage or {}).get("topic_guided_review_sha256", "")),
                "topic_guided_review_hashes": dict((safe_lineage or {}).get("topic_guided_review_hashes", {}) or {}),
                "max_total_calls": TOPIC_GUIDED_MAX_TOTAL_CALLS,
                "provider_call_limit": TOPIC_GUIDED_MAX_TOTAL_CALLS,
                "selected_page_limit": TOPIC_GUIDED_MAX_SELECTED_PAGES,
                "per_page_provider_call_limit": TOPIC_GUIDED_PER_PAGE_PROVIDER_CALL_LIMIT,
                "retry_count": TOPIC_GUIDED_MAX_RETRIES,
                "supplement_calls": TOPIC_GUIDED_SUPPLEMENT_CALLS,
                "supplement_pages": TOPIC_GUIDED_SUPPLEMENT_PAGES,
                "health_calls": TOPIC_GUIDED_HEALTH_CALLS,
                "sdk_calls": TOPIC_GUIDED_SDK_CALLS,
                "no_supplement": True,
                "response_format_mode": RESPONSE_FORMAT_MODE,
                "response_format_sent": False,
                "thinking_disabled": THINKING_DISABLED,
                "health_required": False,
                "stage_b_authorized": False,
                "stage_c_authorized": False,
                "production_authorized": False,
                "frozen_read": False,
                "gold_loaded": False,
            }
        )
    if authorization_contract:
        manifest["authorization_contract"] = _body_free(dict(authorization_contract))
    if safe_lineage:
        manifest["lineage"] = safe_lineage
        for key in (
            "authorization_id",
            "adapter",
            "adapter_source",
            "adapter_sha256",
            "adapter_code_sha256",
            "code_sha256",
            "source_code_sha256",
            "selection_sha256",
            "context_input_sha256",
            "audit_summary_sha256",
            "human_audit_sha256",
            "candidate_only",
            "not_canonical",
            "semantic_decision_pending",
            "provider_primary_role",
            "settings_sha256",
            "model",
            "protocol",
            "scope_sha256",
            "max_provider_calls",
            "per_page_provider_call_limit",
            "max_retries",
            "audit_authorization_source",
            "stage_b_authorized",
            "stage_c_authorized",
            "production_authorized",
            "stratified_current",
            "subset_sha256",
            "guard_audit_summary_sha256",
            "guard_human_audit_sha256",
            "guards_replay_sha256",
            "guard_prompt_version",
            "guard_taxonomy_version",
            "guard_code_sha256",
            "guard_page_refs",
            "guard_ids",
            "guarded_authorization",
            "authority_bound_authorization",
            "authority_bound",
            "authority_projection_hashes",
            "authority_projection_binding_sha256",
            "authority_projection_page_refs",
            "authority_projection_scope_ref",
            "authority_bound_canonical_mapping_version",
            "authority_bound_canonical_mapping_sha256",
            "authority_bound_canonical_mapping",
            "authority_bound_code_sha256",
            "authority_bound_prompt_version",
            "authority_bound_prompt_sha256",
            "authority_bound_taxonomy_version",
            "topic_guided_authorization",
            "topic_guided",
            "authority_bound_projection",
            "topic_guided_code_sha256",
            "topic_guided_prompt_version",
            "topic_guided_prompt_sha256",
            "topic_guided_guidance_version",
            "topic_guided_guidance_sha256",
            "topic_guided_grouping_hints_version",
            "topic_guided_grouping_hints_policy_sha256",
            "topic_guided_g_hint_binding_sha256",
            "topic_guided_protocol_source_sha256",
            "topic_guided_taxonomy_version",
            "topic_guided_review_hashes",
            "topic_guided_review_sha256",
            "max_total_calls",
            "health_calls",
            "supplement_calls",
            "supplement_pages",
            "no_supplement",
        ):
            if key in safe_lineage:
                manifest[key] = safe_lineage[key]
    values: Dict[str, Any] = {
        "aggregate": aggregate,
        "cost": cost,
        "ledger": ledger_rows,
        "selection": selection_rows,
        "decisions": decision_rows,
        "diagnostics": diagnostic_rows,
        "errors": errors,
    }
    for label, value in values.items():
        _body_free(value)
    _body_free(manifest)
    if output_root.exists():
        raise FileExistsError("development_v3_output_is_immutable")
    output_root.mkdir(parents=True, exist_ok=False)
    _write_json(output_root / OUTPUT_FILENAMES["aggregate"], aggregate)
    _write_json(output_root / OUTPUT_FILENAMES["cost"], cost)
    _write_jsonl(output_root / OUTPUT_FILENAMES["ledger"], ledger_rows)
    _write_jsonl(output_root / OUTPUT_FILENAMES["selection"], selection_rows)
    _write_jsonl(output_root / OUTPUT_FILENAMES["decisions"], decision_rows)
    _write_jsonl(output_root / OUTPUT_FILENAMES["diagnostics"], diagnostic_rows)
    _write_jsonl(output_root / OUTPUT_FILENAMES["errors"], errors)
    manifest["artifact_hashes"] = {
        filename: _file_sha256(output_root / filename)
        for filename in OUTPUT_FILENAMES.values()
        if filename != OUTPUT_FILENAMES["manifest"]
    }
    _write_json(output_root / OUTPUT_FILENAMES["manifest"], manifest)
    paths = {key: str(output_root / filename) for key, filename in OUTPUT_FILENAMES.items()}
    return paths, aggregate, status, success


def _health_metadata(path: Optional[Union[str, Path]]) -> Tuple[bool, Dict[str, Any]]:
    if path is None:
        return False, {"status": "not_supplied", "strict_complete": False, "calls": 0}
    root = _safe_path(path, "input_artifact_invalid")
    manifest_path = root / "manifest.private.json"
    aggregate_path = root / "aggregate.private.json"
    if not manifest_path.is_file() or not aggregate_path.is_file():
        return False, {"status": "missing", "strict_complete": False, "calls": 0}
    manifest = _json_read(manifest_path, "input_manifest_invalid")
    aggregate = _json_read(aggregate_path, "input_manifest_invalid")
    provider = manifest.get("provider") if isinstance(manifest.get("provider"), Mapping) else {}
    strict = aggregate.get("strict_result") if isinstance(aggregate.get("strict_result"), Mapping) else {}
    ok = bool(
        manifest.get("status") == "available"
        and manifest.get("artifact_version") in {"compact_stage_a_protocol_health_v3", "compact_stage_a_protocol_health_v3"}
        and manifest.get("frozen_read") is False
        and manifest.get("development_input_read") is False
        and manifest.get("production_state_written") is False
        and manifest.get("thinking_disabled") is True
        and manifest.get("provider_calls") == 1
        and provider.get("model") == MODEL_ID
        and provider.get("response_format_sent") is False
        and (strict.get("strict_complete") is True or aggregate.get("health_complete") is True)
    )
    return ok, {"status": str(manifest.get("status", "unknown")), "strict_complete": bool(strict.get("strict_complete")), "calls": int(manifest.get("provider_calls") or 0), "model": str(provider.get("model", ""))}


def _is_stratified_current_path(path: Path) -> bool:
    if path.name.casefold() == INPUT_ARTIFACT_VERSION.casefold():
        return True
    manifest_path = path / "manifest.private.json"
    if not manifest_path.is_file():
        return False
    try:
        manifest = _json_read(manifest_path, "input_manifest_invalid")
    except CompactStageADevelopmentError:
        return False
    return manifest.get("artifact_version") == STRATIFIED_SOURCE_ARTIFACT_VERSION


def _stratified_input_binding_hash(
    manifest: Mapping[str, Any],
    hashes: Mapping[str, str],
    lineage: Mapping[str, Any],
    selected: Sequence[Tuple[Mapping[str, Any], Tuple[str, ...]]],
) -> str:
    """Bind the global selection, K2 input, settings-independent hashes and scope."""

    return stable_hash(
        {
            "source_artifact_version": manifest.get("artifact_version"),
            "source_manifest_sha256": hashes.get("source_manifest_sha256", ""),
            "source_selection_sha256": lineage.get("selection_sha256", ""),
            "context_input_sha256": lineage.get("context_input_sha256", ""),
            "context_manifest_sha256": hashes.get("context_manifest_sha256", ""),
            "context_packets_sha256": hashes.get("context_packets_private_jsonl_sha256", ""),
            "audit_summary_sha256": lineage.get("audit_summary_sha256", ""),
            "human_audit_sha256": lineage.get("human_audit_sha256", ""),
            "selected": [
                {
                    "page_id": str(page.get("page_id", "")),
                    "root_id": str(page.get("root_id", "")),
                    "source_handle": str(page.get("_source_handle", "")),
                    "selection_rank": int(page.get("_selection_rank", index + 1)),
                    "page_hash": str(page.get("page_hash", "")),
                    "scope": _scope(page.get("scope")),
                    "categories": list(categories),
                    "candidate_only": bool(page.get("_candidate_only", False)),
                    "not_canonical": bool(page.get("_not_canonical", False)),
                    "semantic_decision_pending": bool(page.get("_semantic_decision_pending", False)),
                }
                for index, (page, categories) in enumerate(selected)
            ],
        }
    )


def _adapter_fix_input_binding_hash(
    manifest: Mapping[str, Any],
    hashes: Mapping[str, str],
    lineage: Mapping[str, Any],
    selected: Sequence[Tuple[Mapping[str, Any], Tuple[str, ...]]],
    *,
    authorization_id: str,
    adapter_metadata: Mapping[str, str],
    model: str,
    protocol: str,
    settings_sha256: str,
    scope: Mapping[str, str],
) -> str:
    """Build the repaired authorization's complete, body-free binding.

    The legacy authorization deliberately keeps its historical input binding
    (and therefore keeps its old ledger address/identity).  This separate
    binding includes every source and execution-contract component that may
    change the meaning of a repaired run.
    """

    normalized_scope = _scope(scope)
    return stable_hash(
        {
            "binding_schema": "compact_stage_a_adapter_fix_authorization_v1",
            "authorization_id": authorization_id,
            "source_artifact_version": manifest.get("artifact_version"),
            "source_manifest_sha256": hashes.get("source_manifest_sha256", hashes.get("manifest_sha256", "")),
            "source_selection_sha256": lineage.get("selection_sha256", ""),
            "context_input_sha256": lineage.get("context_input_sha256", hashes.get("context_input_sha256", "")),
            "context_manifest_sha256": hashes.get("context_manifest_sha256", ""),
            "context_packets_sha256": hashes.get("context_packets_private_jsonl_sha256", ""),
            "audit_summary_sha256": lineage.get("audit_summary_sha256", ""),
            "human_audit_sha256": lineage.get("human_audit_sha256", ""),
            "selected": [
                {
                    "page_id": str(page.get("page_id", "")),
                    "root_id": str(page.get("root_id", "")),
                    "source_handle": str(page.get("_source_handle", "")),
                    "selection_rank": int(page.get("_selection_rank", index + 1)),
                    "page_hash": str(page.get("page_hash", "")),
                    "scope": _scope(page.get("scope")),
                    "categories": list(categories),
                }
                for index, (page, categories) in enumerate(selected)
            ],
            "adapter": dict(adapter_metadata),
            "model": model,
            "protocol": protocol,
            "settings_sha256": settings_sha256,
            "scope": normalized_scope,
            "scope_sha256": stable_hash(normalized_scope),
            "source_code_sha256": _code_hash_fingerprint(manifest.get("code_sha256", "")),
            "limits": {
                "max_provider_calls": MAX_PROVIDER_CALLS,
                "per_page_provider_call_limit": PER_PAGE_PROVIDER_CALL_LIMIT,
                "max_retries": MAX_RETRIES,
            },
        }
    )


def _guard_validation_input_binding_hash(
    manifest: Mapping[str, Any],
    hashes: Mapping[str, str],
    lineage: Mapping[str, Any],
    selected: Sequence[Tuple[Mapping[str, Any], Tuple[str, ...]]],
    *,
    authorization_id: str,
    subset_sha256: str,
    subset: Sequence[Mapping[str, Any]],
    guard_audit_hashes: Mapping[str, str],
    adapter_metadata: Mapping[str, str],
    model: str,
    protocol: str,
    settings_sha256: str,
    scope: Mapping[str, str],
    guard_code_sha256: str,
) -> str:
    """Bind the guard run to current inputs, code and the exact three refs."""

    normalized_scope = _scope(scope)
    selected_rows = [
        {
            "page_id": str(page.get("page_id", "")),
            "root_id": str(page.get("root_id", "")),
            "source_handle": str(page.get("_source_handle", "")),
            "selection_rank": int(page.get("_selection_rank", index + 1)),
            "page_hash": str(page.get("page_hash", "")),
            "scope": _scope(page.get("scope")),
            "categories": list(categories),
            "guard_id": str(page.get("_guard_id", "")),
        }
        for index, (page, categories) in enumerate(selected)
    ]
    return stable_hash(
        {
            "binding_schema": "compact_stage_a_guard_validation_authorization_v1",
            "authorization_id": authorization_id,
            "artifact_version": GUARD_VALIDATION_ARTIFACT_VERSION,
            "artifact_namespace": GUARD_VALIDATION_ARTIFACT_NAMESPACE,
            "source_artifact_version": manifest.get("artifact_version"),
            "source_hashes": dict(sorted((str(key), str(value)) for key, value in hashes.items())),
            "source_selection_sha256": lineage.get("selection_sha256", ""),
            "source_input_selected_digest": manifest.get("input_selected_digest", ""),
            "source_input_packet_digest": manifest.get("input_packet_digest", ""),
            "context_input_sha256": lineage.get("context_input_sha256", hashes.get("context_input_sha256", "")),
            "audit_summary_sha256": lineage.get("audit_summary_sha256", ""),
            "human_audit_sha256": lineage.get("human_audit_sha256", ""),
            "guard_audit_hashes": dict(sorted((str(key), str(value)) for key, value in guard_audit_hashes.items())),
            "subset_sha256": subset_sha256,
            "subset": [dict(row) for row in subset],
            "selected": selected_rows,
            "adapter": dict(adapter_metadata),
            "guard_code_sha256": guard_code_sha256,
            "guard_prompt_version": GUARD_VALIDATION_PROMPT_VERSION,
            "guard_prompt_sha256": stable_hash(GUARD_VALIDATION_SYSTEM_PROMPT),
            "guard_taxonomy_version": GUARD_VALIDATION_TAXONOMY_VERSION,
            "model": model,
            "protocol": protocol,
            "settings_sha256": settings_sha256,
            "scope": normalized_scope,
            "scope_sha256": stable_hash(normalized_scope),
            "limits": {
                "max_provider_calls": GUARD_VALIDATION_MAX_PROVIDER_CALLS,
                "selected_page_limit": GUARD_VALIDATION_MAX_SELECTED_PAGES,
                "per_page_provider_call_limit": GUARD_VALIDATION_PER_PAGE_PROVIDER_CALL_LIMIT,
                "max_retries": GUARD_VALIDATION_MAX_RETRIES,
                "sdk_calls": GUARD_VALIDATION_SDK_CALLS,
            },
        }
    )


def _guarded_input_binding_hash(
    manifest: Mapping[str, Any],
    hashes: Mapping[str, str],
    lineage: Mapping[str, Any],
    selected: Sequence[Tuple[Mapping[str, Any], Tuple[str, ...]]],
    *,
    authorization_id: str,
    adapter_metadata: Mapping[str, str],
    model: str,
    protocol: str,
    settings_sha256: str,
    scope: Mapping[str, str],
    guard_code_sha256: str,
) -> str:
    """Bind the guarded pilot to the reviewed current five-page contract.

    This binding deliberately carries the same selection, current audit and
    context hashes used by the adapter-fix authorization, then adds the
    guarded code/prompt policy and execution identity.  It contains only
    opaque refs, hashes, enums and bounded limits; request/message bodies are
    never included.
    """

    normalized_scope = _scope(scope)
    selected_rows = [
        {
            "page_id": str(page.get("page_id", "")),
            "root_id": str(page.get("root_id", "")),
            "source_handle": str(page.get("_source_handle", page.get("source_handle", ""))),
            "scope_handle": str(page.get("_scope_handle", page.get("scope_handle", ""))),
            "selection_rank": int(page.get("_selection_rank", index + 1)),
            "page_hash": str(page.get("page_hash", "")),
            "scope": _scope(page.get("scope")),
            "categories": list(categories),
        }
        for index, (page, categories) in enumerate(selected)
    ]
    return stable_hash(
        {
            "binding_schema": "compact_stage_a_guarded_authorization_v1",
            "authorization_id": authorization_id,
            "artifact_version": GUARDED_ARTIFACT_VERSION,
            "artifact_namespace": GUARDED_ARTIFACT_NAMESPACE,
            "source_artifact_version": manifest.get("artifact_version"),
            "source_hashes": dict(sorted((str(key), str(value)) for key, value in hashes.items())),
            "source_selection_sha256": str(lineage.get("selection_sha256", "")),
            "selection_sha256": str(lineage.get("selection_sha256", "")),
            "context_input_sha256": str(lineage.get("context_input_sha256", hashes.get("context_input_sha256", ""))),
            "context_manifest_sha256": str(hashes.get("context_manifest_sha256", "")),
            "context_packets_sha256": str(hashes.get("context_packets_private_jsonl_sha256", "")),
            "current_context_sha256": str(lineage.get("context_input_sha256", hashes.get("context_input_sha256", ""))),
            "audit_summary_sha256": str(lineage.get("audit_summary_sha256", "")),
            "human_audit_sha256": str(lineage.get("human_audit_sha256", "")),
            "current_audit_sha256": str(lineage.get("audit_summary_sha256", "")),
            "current_human_audit_sha256": str(lineage.get("human_audit_sha256", "")),
            "selected": selected_rows,
            "selected_page_refs": [row["page_id"] for row in selected_rows],
            "selected_root_refs": [row["root_id"] for row in selected_rows],
            "selected_source_refs": [row["source_handle"] for row in selected_rows],
            "adapter": dict(adapter_metadata),
            "adapter_sha256": str(adapter_metadata.get("adapter_sha256", "")),
            "adapter_code_sha256": str(adapter_metadata.get("adapter_code_sha256", "")),
            "runner_code_sha256": str(adapter_metadata.get("code_sha256", "")),
            "source_code_sha256": _code_hash_fingerprint(manifest.get("code_sha256", "")),
            "guard_code_sha256": guard_code_sha256,
            "guard_prompt_version": GUARDED_PROMPT_VERSION,
            "guard_prompt_sha256": stable_hash(GUARDED_SYSTEM_PROMPT),
            "guard_taxonomy_version": GUARDED_TAXONOMY_VERSION,
            "model": model,
            "model_sha256": stable_hash(model),
            "protocol": protocol,
            "protocol_sha256": stable_hash(protocol),
            "settings_sha256": settings_sha256,
            "scope": normalized_scope,
            "scope_sha256": stable_hash(normalized_scope),
            "limits": {
                "max_provider_calls": GUARDED_MAX_PROVIDER_CALLS,
                "selected_page_limit": GUARDED_MAX_SELECTED_PAGES,
                "per_page_provider_call_limit": GUARDED_PER_PAGE_PROVIDER_CALL_LIMIT,
                "max_retries": GUARDED_MAX_RETRIES,
                "sdk_calls": GUARDED_SDK_CALLS,
                "supplement_calls": 0,
                "supplement_pages": 0,
            },
            "no_supplement": True,
        }
    )


def _adapter_fix_contract(
    *,
    authorization_id: str,
    adapter_metadata: Mapping[str, str],
    model: str,
    protocol: str,
    settings_sha256: str,
    scope: Mapping[str, str],
    lineage: Mapping[str, Any],
    input_binding_sha256: str,
    source_code_sha256: str = "",
) -> Dict[str, Any]:
    """Return the persisted scalar contract for the repaired authorization."""

    normalized_scope = _scope(scope)
    return {
        "authorization_id": authorization_id,
        "adapter": str(adapter_metadata.get("adapter", "unconfigured")),
        "adapter_source": str(adapter_metadata.get("adapter_source", SOURCE_ID)),
        "adapter_sha256": str(adapter_metadata.get("adapter_sha256", "")),
        "adapter_code_sha256": str(adapter_metadata.get("adapter_code_sha256", "")),
        "code_sha256": str(adapter_metadata.get("code_sha256", "")),
        "source_code_sha256": str(source_code_sha256 or ""),
        "protocol": protocol,
        "settings_sha256": settings_sha256,
        "model": model,
        "scope": normalized_scope,
        "scope_sha256": stable_hash(normalized_scope),
        "selection_sha256": str(lineage.get("selection_sha256", "")),
        "audit_summary_sha256": str(lineage.get("audit_summary_sha256", "")),
        "human_audit_sha256": str(lineage.get("human_audit_sha256", "")),
        "context_input_sha256": str(lineage.get("context_input_sha256", "")),
        "input_binding_sha256": input_binding_sha256,
        "max_provider_calls": MAX_PROVIDER_CALLS,
        "per_page_provider_call_limit": PER_PAGE_PROVIDER_CALL_LIMIT,
        "max_retries": MAX_RETRIES,
        "body_free": True,
    }


def _guard_validation_contract(
    *,
    authorization_id: str,
    adapter_metadata: Mapping[str, str],
    model: str,
    protocol: str,
    settings_sha256: str,
    scope: Mapping[str, str],
    lineage: Mapping[str, Any],
    input_binding_sha256: str,
    source_code_sha256: str = "",
) -> Dict[str, Any]:
    """Return the body-free contract for the explicit three-page guard run."""

    normalized_scope = _scope(scope)
    return {
        "authorization_id": authorization_id,
        "artifact_version": GUARD_VALIDATION_ARTIFACT_VERSION,
        "artifact_namespace": GUARD_VALIDATION_ARTIFACT_NAMESPACE,
        "adapter": str(adapter_metadata.get("adapter", "unconfigured")),
        "adapter_source": str(adapter_metadata.get("adapter_source", SOURCE_ID)),
        "adapter_sha256": str(adapter_metadata.get("adapter_sha256", "")),
        "adapter_code_sha256": str(adapter_metadata.get("adapter_code_sha256", "")),
        "code_sha256": str(adapter_metadata.get("code_sha256", "")),
        "source_code_sha256": str(source_code_sha256 or ""),
        "protocol": protocol,
        "settings_sha256": settings_sha256,
        "model": model,
        "scope": normalized_scope,
        "scope_sha256": stable_hash(normalized_scope),
        "selection_sha256": str(lineage.get("selection_sha256", "")),
        "audit_summary_sha256": str(lineage.get("audit_summary_sha256", "")),
        "human_audit_sha256": str(lineage.get("human_audit_sha256", "")),
        "context_input_sha256": str(lineage.get("context_input_sha256", "")),
        "guard_audit_summary_sha256": str(lineage.get("guard_audit_summary_sha256", "")),
        "guard_human_audit_sha256": str(lineage.get("guard_human_audit_sha256", "")),
        "guards_replay_sha256": str(lineage.get("guards_replay_sha256", "")),
        "subset_sha256": str(lineage.get("subset_sha256", "")),
        "guard_code_sha256": str(lineage.get("guard_code_sha256", "")),
        "guard_prompt_version": GUARD_VALIDATION_PROMPT_VERSION,
        "guard_prompt_sha256": stable_hash(GUARD_VALIDATION_SYSTEM_PROMPT),
        "guard_taxonomy_version": GUARD_VALIDATION_TAXONOMY_VERSION,
        "guard_page_refs": list(lineage.get("guard_page_refs", ())),
        "guard_ids": list(lineage.get("guard_ids", ())),
        "input_binding_sha256": input_binding_sha256,
        "max_provider_calls": GUARD_VALIDATION_MAX_PROVIDER_CALLS,
        "selected_page_limit": GUARD_VALIDATION_MAX_SELECTED_PAGES,
        "per_page_provider_call_limit": GUARD_VALIDATION_PER_PAGE_PROVIDER_CALL_LIMIT,
        "max_retries": GUARD_VALIDATION_MAX_RETRIES,
        "sdk_calls": GUARD_VALIDATION_SDK_CALLS,
        "body_free": True,
    }


def _guarded_contract(
    *,
    authorization_id: str,
    adapter_metadata: Mapping[str, str],
    model: str,
    protocol: str,
    settings_sha256: str,
    scope: Mapping[str, str],
    lineage: Mapping[str, Any],
    input_binding_sha256: str,
    guard_code_sha256: str,
    selected: Sequence[Tuple[Mapping[str, Any], Tuple[str, ...]]] = (),
    source_code_sha256: str = "",
) -> Dict[str, Any]:
    """Return the body-free contract for the guarded five-page pilot."""

    normalized_scope = _scope(scope)
    selected_rows = [
        {
            "page_id": str(page.get("page_id", "")),
            "root_id": str(page.get("root_id", "")),
            "source_handle": str(page.get("_source_handle", page.get("source_handle", ""))),
            "scope_handle": str(page.get("_scope_handle", page.get("scope_handle", ""))),
            "selection_rank": int(page.get("_selection_rank", index + 1)),
            "page_hash": str(page.get("page_hash", "")),
        }
        for index, (page, _categories) in enumerate(selected)
    ]
    return {
        "authorization_id": authorization_id,
        "artifact_version": GUARDED_ARTIFACT_VERSION,
        "artifact_namespace": GUARDED_ARTIFACT_NAMESPACE,
        "adapter": str(adapter_metadata.get("adapter", "unconfigured")),
        "adapter_source": str(adapter_metadata.get("adapter_source", SOURCE_ID)),
        "adapter_sha256": str(adapter_metadata.get("adapter_sha256", "")),
        "adapter_code_sha256": str(adapter_metadata.get("adapter_code_sha256", "")),
        "code_sha256": str(adapter_metadata.get("code_sha256", "")),
        "source_code_sha256": str(source_code_sha256 or ""),
        "guard_code_sha256": guard_code_sha256,
        "guard_prompt_version": GUARDED_PROMPT_VERSION,
        "guard_prompt_sha256": stable_hash(GUARDED_SYSTEM_PROMPT),
        "guard_taxonomy_version": GUARDED_TAXONOMY_VERSION,
        "protocol": protocol,
        "protocol_sha256": stable_hash(protocol),
        "settings_sha256": settings_sha256,
        "model": model,
        "model_sha256": stable_hash(model),
        "scope": normalized_scope,
        "scope_sha256": stable_hash(normalized_scope),
        "selection_sha256": str(lineage.get("selection_sha256", "")),
        "audit_summary_sha256": str(lineage.get("audit_summary_sha256", "")),
        "human_audit_sha256": str(lineage.get("human_audit_sha256", "")),
        "context_input_sha256": str(lineage.get("context_input_sha256", "")),
        # Explicit current-* aliases make the provenance contract readable to
        # audit consumers while preserving the historical field names.
        "current_audit_sha256": str(lineage.get("audit_summary_sha256", "")),
        "current_human_audit_sha256": str(lineage.get("human_audit_sha256", "")),
        "current_context_sha256": str(lineage.get("context_input_sha256", "")),
        "selected_page_refs": [row["page_id"] for row in selected_rows],
        "selected_root_refs": [row["root_id"] for row in selected_rows],
        "selected_source_refs": [row["source_handle"] for row in selected_rows],
        "guard_page_refs": [row["page_id"] for row in selected_rows],
        "max_provider_calls": GUARDED_MAX_PROVIDER_CALLS,
        "selected_page_limit": GUARDED_MAX_SELECTED_PAGES,
        "per_page_provider_call_limit": GUARDED_PER_PAGE_PROVIDER_CALL_LIMIT,
        "max_retries": GUARDED_MAX_RETRIES,
        "sdk_calls": GUARDED_SDK_CALLS,
        "supplement_calls": 0,
        "supplement_pages": 0,
        "no_supplement": True,
        "input_binding_sha256": input_binding_sha256,
        "body_free": True,
    }


def _authority_bound_input_binding_hash(
    manifest: Mapping[str, Any],
    hashes: Mapping[str, str],
    lineage: Mapping[str, Any],
    selected: Sequence[Tuple[Mapping[str, Any], Tuple[str, ...]]],
    *,
    authorization_id: str,
    adapter_metadata: Mapping[str, str],
    model: str,
    protocol: str,
    settings_sha256: str,
    scope: Mapping[str, str],
    authority_projection_hashes: Mapping[str, str],
    authority_projection_page_refs: Sequence[str],
) -> str:
    """Bind a run to the current selection and authority projection audit.

    Every value is an opaque id, digest, enum or bounded scalar.  The
    authority projection itself is never copied into this binding and no
    provider request/response body can cross this boundary.
    """

    normalized_scope = _scope(scope)
    selected_rows = [
        {
            "page_id": str(page.get("page_id", "")),
            "root_id": str(page.get("root_id", "")),
            "source_handle": str(page.get("_source_handle", page.get("source_handle", ""))),
            "scope_handle": str(page.get("_scope_handle", page.get("scope_handle", ""))),
            "selection_rank": int(page.get("_selection_rank", index + 1)),
            "page_hash": str(page.get("page_hash", "")),
            "scope": _scope(page.get("scope")),
            "categories": list(categories),
            "authority_projection_sha256": str(
                page.get("_authority_projection_sha256")
                or page.get("authority_projection_sha256")
                or page.get("_authority_bound_sha256")
                or ""
            ),
        }
        for index, (page, categories) in enumerate(selected)
    ]
    return stable_hash(
        {
            "binding_schema": "compact_stage_a_authority_bound_authorization_v1",
            "authorization_id": authorization_id,
            "artifact_version": AUTHORITY_BOUND_ARTIFACT_VERSION,
            "artifact_namespace": AUTHORITY_BOUND_ARTIFACT_NAMESPACE,
            "source_artifact_version": manifest.get("artifact_version"),
            "source_hashes": dict(sorted((str(key), str(value)) for key, value in hashes.items())),
            "selection_sha256": str(lineage.get("selection_sha256", "")),
            "context_input_sha256": str(lineage.get("context_input_sha256", "")),
            "audit_summary_sha256": str(lineage.get("audit_summary_sha256", "")),
            "human_audit_sha256": str(lineage.get("human_audit_sha256", "")),
            "authority_projection_hashes": dict(
                sorted((str(key), str(value)) for key, value in authority_projection_hashes.items())
            ),
            "authority_bound_canonical_mapping_version": AUTHORITY_BOUND_CANONICAL_MAPPING_VERSION,
            "authority_bound_canonical_mapping_sha256": AUTHORITY_BOUND_CANONICAL_MAPPING_SHA256,
            "authority_bound_canonical_mapping": [
                dict(row) for row in AUTHORITY_BOUND_CANONICAL_MAPPING
            ],
            "authority_projection_page_refs": [str(item) for item in authority_projection_page_refs],
            "authority_projection_binding_sha256": str(lineage.get("authority_projection_binding_sha256", "")),
            "selected": selected_rows,
            "adapter": dict(adapter_metadata),
            "runner_code_sha256": str(adapter_metadata.get("code_sha256", "")),
            "source_code_sha256": _code_hash_fingerprint(manifest.get("code_sha256", "")),
            "authority_bound_code_sha256": str(lineage.get("authority_bound_code_sha256", "")),
            "authority_bound_prompt_version": AUTHORITY_BOUND_PROMPT_VERSION,
            "authority_bound_prompt_sha256": stable_hash(AUTHORITY_BOUND_SYSTEM_PROMPT),
            "authority_bound_taxonomy_version": AUTHORITY_BOUND_TAXONOMY_VERSION,
            "model": model,
            "model_sha256": stable_hash(model),
            "protocol": protocol,
            "protocol_sha256": stable_hash(protocol),
            "settings_sha256": settings_sha256,
            "scope": normalized_scope,
            "scope_sha256": stable_hash(normalized_scope),
            "limits": {
                "max_total_calls": AUTHORITY_BOUND_MAX_TOTAL_CALLS,
                "max_provider_calls": AUTHORITY_BOUND_MAX_PROVIDER_CALLS,
                "selected_page_limit": AUTHORITY_BOUND_MAX_SELECTED_PAGES,
                "per_page_provider_call_limit": AUTHORITY_BOUND_PER_PAGE_PROVIDER_CALL_LIMIT,
                "max_retries": AUTHORITY_BOUND_MAX_RETRIES,
                "supplement_calls": AUTHORITY_BOUND_SUPPLEMENT_CALLS,
                "supplement_pages": AUTHORITY_BOUND_SUPPLEMENT_PAGES,
                "health_calls": AUTHORITY_BOUND_HEALTH_CALLS,
                "sdk_calls": AUTHORITY_BOUND_SDK_CALLS,
            },
            "response_format_mode": RESPONSE_FORMAT_MODE,
            "response_format_sent": False,
            "thinking_disabled": THINKING_DISABLED,
            "stage_b_authorized": False,
            "stage_c_authorized": False,
            "production_authorized": False,
            "frozen_read": False,
            "gold_loaded": False,
            "no_supplement": True,
        }
    )


def _authority_bound_contract(
    *,
    authorization_id: str,
    adapter_metadata: Mapping[str, str],
    model: str,
    protocol: str,
    settings_sha256: str,
    scope: Mapping[str, str],
    lineage: Mapping[str, Any],
    input_binding_sha256: str,
    selected: Sequence[Tuple[Mapping[str, Any], Tuple[str, ...]]] = (),
    source_code_sha256: str = "",
) -> Dict[str, Any]:
    """Return the immutable body-free authority-bound authorization contract."""

    normalized_scope = _scope(scope)
    selected_rows = [
        {
            "selection_rank": int(page.get("_selection_rank", index + 1)),
            "page_ref": str(page.get("page_id", "")),
            "root_ref": str(page.get("root_id", "")),
            "source_ref": str(page.get("_source_handle", page.get("source_handle", ""))),
            "scope_ref": str(page.get("_scope_handle", page.get("scope_handle", ""))),
            "page_hash": str(page.get("page_hash", "")),
        }
        for index, (page, _categories) in enumerate(selected)
    ]
    hashes = {
        str(key): str(value)
        for key, value in (lineage.get("authority_projection_hashes", {}) or {}).items()
    }
    return {
        "binding_schema": "compact_stage_a_authority_bound_authorization_v1",
        "authorization_id": authorization_id,
        "artifact_version": AUTHORITY_BOUND_ARTIFACT_VERSION,
        "artifact_namespace": AUTHORITY_BOUND_ARTIFACT_NAMESPACE,
        "authority_bound_authorization": True,
        "authority_bound": True,
        "adapter": str(adapter_metadata.get("adapter", "unconfigured")),
        "adapter_source": str(adapter_metadata.get("adapter_source", SOURCE_ID)),
        "adapter_sha256": str(adapter_metadata.get("adapter_sha256", "")),
        "adapter_code_sha256": str(adapter_metadata.get("adapter_code_sha256", "")),
        "code_sha256": str(adapter_metadata.get("code_sha256", "")),
        "source_code_sha256": str(source_code_sha256 or ""),
        "authority_bound_code_sha256": str(lineage.get("authority_bound_code_sha256", "")),
        "authority_bound_prompt_version": AUTHORITY_BOUND_PROMPT_VERSION,
        "authority_bound_prompt_sha256": stable_hash(AUTHORITY_BOUND_SYSTEM_PROMPT),
        "authority_bound_taxonomy_version": AUTHORITY_BOUND_TAXONOMY_VERSION,
        "protocol": protocol,
        "protocol_sha256": stable_hash(protocol),
        "settings_sha256": settings_sha256,
        "model": model,
        "model_sha256": stable_hash(model),
        "scope": normalized_scope,
        "scope_sha256": stable_hash(normalized_scope),
        "selection_sha256": str(lineage.get("selection_sha256", "")),
        "context_input_sha256": str(lineage.get("context_input_sha256", "")),
        "audit_summary_sha256": str(lineage.get("audit_summary_sha256", "")),
        "human_audit_sha256": str(lineage.get("human_audit_sha256", "")),
        "authority_projection_hashes": hashes,
        "authority_bound_canonical_mapping_version": AUTHORITY_BOUND_CANONICAL_MAPPING_VERSION,
        "authority_bound_canonical_mapping_sha256": AUTHORITY_BOUND_CANONICAL_MAPPING_SHA256,
        "authority_bound_canonical_mapping": [
            dict(row) for row in AUTHORITY_BOUND_CANONICAL_MAPPING
        ],
        "authority_projection_audit_sha256": str(hashes.get("authority_projection_audit_sha256", "")),
        "authority_projection_code_sha256": str(hashes.get("authority_projection_code_sha256", "")),
        "authority_projection_selection_sha256": str(hashes.get("authority_projection_selection_sha256", "")),
        "authority_projection_context_sha256": str(hashes.get("authority_projection_context_sha256", "")),
        "authority_projection_audit_summary_sha256": str(hashes.get("authority_projection_audit_summary_sha256", "")),
        "authority_projection_human_audit_sha256": str(hashes.get("authority_projection_human_audit_sha256", "")),
        "authority_projection_binding_sha256": str(lineage.get("authority_projection_binding_sha256", "")),
        "authority_projection_page_refs": list(lineage.get("authority_projection_page_refs", ())),
        "authority_projection_scope_ref": str(lineage.get("authority_projection_scope_ref", "")),
        "selected_page_count": AUTHORITY_BOUND_MAX_SELECTED_PAGES,
        "selected_page_limit": AUTHORITY_BOUND_MAX_SELECTED_PAGES,
        "selected_page_refs": [row["page_ref"] for row in selected_rows],
        "selected_root_refs": [row["root_ref"] for row in selected_rows],
        "selected_source_refs": [row["source_ref"] for row in selected_rows],
        "max_total_calls": AUTHORITY_BOUND_MAX_TOTAL_CALLS,
        "max_provider_calls": AUTHORITY_BOUND_MAX_PROVIDER_CALLS,
        "per_page_provider_call_limit": AUTHORITY_BOUND_PER_PAGE_PROVIDER_CALL_LIMIT,
        "max_retries": AUTHORITY_BOUND_MAX_RETRIES,
        "supplement_calls": AUTHORITY_BOUND_SUPPLEMENT_CALLS,
        "supplement_pages": AUTHORITY_BOUND_SUPPLEMENT_PAGES,
        "health_calls": AUTHORITY_BOUND_HEALTH_CALLS,
        "sdk_calls": AUTHORITY_BOUND_SDK_CALLS,
        "response_format_mode": RESPONSE_FORMAT_MODE,
        "response_format_sent": False,
        "thinking_disabled": THINKING_DISABLED,
        "no_supplement": True,
        "stage_b_authorized": False,
        "stage_c_authorized": False,
        "production_authorized": False,
        "frozen_read": False,
        "gold_loaded": False,
        "input_binding_sha256": input_binding_sha256,
        "body_free": True,
    }


def _topic_guided_grouping_policy() -> Dict[str, Any]:
    """Return the body-free grouping-hint policy bound by the new pilot."""

    return {
        "version": TOPIC_GUIDED_GROUPING_HINTS_VERSION,
        "field": GROUPING_HINTS_FIELD,
        "keys": sorted(str(key) for key in TOPIC_HINT_KEYS),
        "relations": list(TOPIC_HINT_RELATION_ORDER),
        "max_hints": MAX_TOPIC_GROUPING_HINTS,
        "max_generated_hints": MAX_GENERATED_TOPIC_GROUPING_HINTS,
        "max_hints_per_alias": MAX_TOPIC_HINTS_PER_ALIAS,
        "max_hints_per_relation": MAX_TOPIC_HINTS_PER_RELATION,
        "max_relations_per_hint": TOPIC_HINT_MAX_RELATIONS,
        "structural_only": True,
        "model_decision_required": True,
    }


def _topic_guided_guidance_sha256() -> str:
    return stable_hash(
        {
            "version": TOPIC_GUIDED_GUIDANCE_VERSION,
            "guidance": TOPIC_GROUPING_GUIDANCE,
            "topic_limit_rule": "topic_count<=primary_count; each primary exactly once",
            "context_rule": "context_unique=global",
            "structural_only": True,
        }
    )


def _topic_guided_grouping_hints_sha256() -> str:
    return stable_hash(_topic_guided_grouping_policy())


def _topic_guided_protocol_source_sha256() -> str:
    """Hash the protocol implementation that builds/validates ``g``."""

    protocol_path = Path(__file__).with_name("compact_stage_a_protocol_v3.py")
    digest = _file_sha256(protocol_path)
    return digest or stable_hash({"protocol": PROTOCOL_VERSION, "source": "unavailable"})


def _topic_guided_code_sha256() -> str:
    """Fingerprint the topic-guided authorization boundary.

    The digest includes the authority projection checks, grouping-hint
    preflight, request builder, artifact writer and runner entry point.  A
    source change therefore cannot silently reuse this fresh authorization.
    """

    source_parts: List[str] = []
    for function in (
        _topic_guided_grouping_policy,
        _topic_guided_guidance_sha256,
        _topic_guided_grouping_hints_sha256,
        _topic_guided_review_page_rows,
        _validate_topic_guided_review,
        _synthetic_topic_guided_review,
        _read_topic_guided_review_evidence,
        _topic_guided_request_stats,
        _topic_guided_validate_requests,
        _topic_guided_input_binding_hash,
        _topic_guided_contract,
        _topic_guided_validate_frozen_boundary,
        _authority_bound_source_mapping,
        _authority_bound_projection_mapping,
        _authority_bound_validate_canonical_mapping,
        _build_request,
        _invoke_model,
        _run_one,
        _write_artifacts,
        run_compact_stage_a_development_pilot_v3,
    ):
        try:
            source_parts.append(inspect.getsource(function))
        except (OSError, TypeError):
            source_parts.append("")
    return stable_hash(
        {
            "prompt_version": TOPIC_GUIDED_PROMPT_VERSION,
            "prompt_sha256": stable_hash(TOPIC_GUIDED_SYSTEM_PROMPT),
            "guidance_version": TOPIC_GUIDED_GUIDANCE_VERSION,
            "guidance_sha256": _topic_guided_guidance_sha256(),
            "grouping_hints_version": TOPIC_GUIDED_GROUPING_HINTS_VERSION,
            "grouping_hints_policy_sha256": _topic_guided_grouping_hints_sha256(),
            "taxonomy_version": TOPIC_GUIDED_TAXONOMY_VERSION,
            "validator_source": "\n".join(source_parts),
            "protocol": PROTOCOL_VERSION,
        }
    )


def _topic_guided_review_page_rows(value: Any) -> Tuple[Mapping[str, Any], ...]:
    """Normalize body-free per-page rows from the latest topic-quality gate."""

    rows = value.get("pages") if isinstance(value, Mapping) else None
    if not isinstance(rows, list) or len(rows) != TOPIC_GUIDED_MAX_SELECTED_PAGES:
        raise CompactStageADevelopmentError("topic_guided_page_drift")
    normalized: List[Mapping[str, Any]] = []
    ranks: List[int] = []
    refs: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise CompactStageADevelopmentError("topic_guided_page_drift")
        try:
            rank = int(row.get("rank", row.get("selection_rank", 0)) or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise CompactStageADevelopmentError("topic_guided_page_drift") from exc
        page_ref = str(row.get("opaque_page_ref") or row.get("page_ref") or "")
        if rank < 1 or rank > TOPIC_GUIDED_MAX_SELECTED_PAGES or not page_ref:
            raise CompactStageADevelopmentError("topic_guided_page_drift")
        if rank in ranks or page_ref in refs:
            raise CompactStageADevelopmentError("topic_guided_page_drift")
        ranks.append(rank)
        refs.add(page_ref)
        normalized.append(dict(row))
    if sorted(ranks) != list(range(1, TOPIC_GUIDED_MAX_SELECTED_PAGES + 1)):
        raise CompactStageADevelopmentError("topic_guided_page_drift")
    normalized.sort(key=lambda row: int(row.get("rank", row.get("selection_rank", 0))))
    return tuple(normalized)


def _validate_topic_guided_review(value: Mapping[str, Any]) -> Tuple[Tuple[Mapping[str, Any], ...], Dict[str, str]]:
    """Validate the latest body-free topic-guided offline gate sidecar."""

    if not isinstance(value, Mapping):
        raise CompactStageADevelopmentError("topic_guided_review_invalid")
    try:
        _body_free(value)
    except CompactStageADevelopmentError as exc:
        raise CompactStageADevelopmentError("topic_guided_review_invalid") from exc
    if value.get("body_free") is False:
        raise CompactStageADevelopmentError("topic_guided_review_invalid")
    if str(value.get("schema") or "") != TOPIC_GUIDED_REVIEW_SCHEMA:
        raise CompactStageADevelopmentError("topic_guided_review_invalid")
    if str(value.get("review_status") or "") != TOPIC_GUIDED_REVIEW_STATUS:
        raise CompactStageADevelopmentError("topic_guided_review_invalid")
    if value.get("gate_pass") is not True:
        raise CompactStageADevelopmentError("topic_guided_review_invalid")

    boundaries = value.get("execution_boundaries") if isinstance(value.get("execution_boundaries"), Mapping) else {}
    for key in (
        "provider_called",
        "authority_bound_runner_invoked",
        "authorization_ledger_opened",
        "frozen_semantics_read",
        "frozen_test_semantics_read",
        "gold_semantics_read",
        "implementation_modified",
        "production_state_written",
    ):
        if boundaries.get(key) is True:
            raise CompactStageADevelopmentError("topic_guided_review_invalid")
    for key in ("provider_call_count", "diagnostic_provider_call_count"):
        if key in boundaries:
            try:
                if int(boundaries.get(key) or 0) != 0:
                    raise CompactStageADevelopmentError("topic_guided_review_invalid")
            except (TypeError, ValueError, OverflowError) as exc:
                raise CompactStageADevelopmentError("topic_guided_review_invalid") from exc

    scope = value.get("selection_scope") if isinstance(value.get("selection_scope"), Mapping) else {}
    if scope:
        if scope.get("selected_page_count") not in (None, TOPIC_GUIDED_MAX_SELECTED_PAGES):
            raise CompactStageADevelopmentError("topic_guided_page_drift")
        for key in ("same_five_page_slice", "same_five_page_materialize", "development_only", "candidate_only", "not_canonical", "semantic_decision_pending", "single_scope"):
            if key in scope and scope.get(key) is not True:
                raise CompactStageADevelopmentError("topic_guided_review_invalid")
        for key in ("scope_count", "cross_scope_message_rows"):
            if key in scope:
                try:
                    expected = 1 if key == "scope_count" else 0
                    if int(scope.get(key) or 0) != expected:
                        raise CompactStageADevelopmentError("topic_guided_scope_drift")
                except (TypeError, ValueError, OverflowError) as exc:
                    raise CompactStageADevelopmentError("topic_guided_scope_drift") from exc

    baseline = value.get("human_true_substantive_baseline") if isinstance(value.get("human_true_substantive_baseline"), Mapping) else {}
    expected_counts = baseline.get("expected_counts")
    observed_counts = baseline.get("observed_counts")
    if expected_counts is not None or observed_counts is not None:
        if list(expected_counts or ()) != list(observed_counts or ()) or baseline.get("exact") is not True:
            raise CompactStageADevelopmentError("topic_guided_page_drift")

    build = value.get("five_page_offline_build") if isinstance(value.get("five_page_offline_build"), Mapping) else {}
    if build:
        if len(list(build.get("primary_counts") or ())) != TOPIC_GUIDED_MAX_SELECTED_PAGES:
            raise CompactStageADevelopmentError("topic_guided_page_drift")
        for key in ("primary_exact_baseline", "all_http_le_1600", "all_output_le_400", "context_strictly_r_c", "context_fallback_used", "context_repair_or_coercion"):
            if key in build:
                expected = False if key in {"context_fallback_used", "context_repair_or_coercion"} else True
                if build.get(key) is not expected:
                    raise CompactStageADevelopmentError("topic_guided_review_invalid")

    grouping = value.get("grouping_hint_review") if isinstance(value.get("grouping_hint_review"), Mapping) else {}
    if grouping:
        try:
            if int(grouping.get("real_page_max_g_rows") or 0) > MAX_TOPIC_GROUPING_HINTS:
                raise CompactStageADevelopmentError("topic_guided_grouping_hint_drift")
        except (TypeError, ValueError, OverflowError) as exc:
            raise CompactStageADevelopmentError("topic_guided_grouping_hint_drift") from exc
        for key in ("real_page_all_g_le_16", "real_page_candidate_auxiliary_hints_only", "dense_synthetic_message_rows_preserved", "dense_synthetic_emitted_is_deterministic_prefix", "dense_synthetic_body_marker_in_g", "representative_witnesses_complete", "generated_hint_selector_does_not_delete_message_rows"):
            expected = False if key == "dense_synthetic_body_marker_in_g" else True
            if key in grouping and grouping.get(key) is not expected:
                raise CompactStageADevelopmentError("topic_guided_grouping_hint_drift")
        relation_families = grouping.get("dense_synthetic_relation_families")
        if relation_families is not None and not set(str(item) for item in relation_families).issubset(set(TOPIC_HINT_RELATIONS)):
            raise CompactStageADevelopmentError("topic_guided_grouping_hint_drift")

    authority = value.get("authority_evidence") if isinstance(value.get("authority_evidence"), Mapping) else {}
    for key in (
        "actual_media_mispromotion_total",
        "actual_empty_authority_mispromotion_total",
        "actual_placeholder_mispromotion_total",
        "actual_unbound_cue_promoted_total",
        "actual_unmarked_system_or_event_mispromotion_total",
        "actual_forbidden_repromotion_total",
    ):
        if key in authority:
            try:
                if int(authority.get(key) or 0) != 0:
                    raise CompactStageADevelopmentError("topic_guided_review_invalid")
            except (TypeError, ValueError, OverflowError) as exc:
                raise CompactStageADevelopmentError("topic_guided_review_invalid") from exc

    acceptance = value.get("acceptance") if isinstance(value.get("acceptance"), Mapping) else {}
    if acceptance:
        for key in ("all_http_le_1600", "all_output_le_400", "primary_exact_baseline", "g_le_16", "g_representative_witnesses", "g_does_not_delete_message_rows", "placeholder_direct_cue_context", "system_event_caption_narrow_exception", "media_mispromotion_zero", "empty_authority_mispromotion_zero", "unbound_promoted_zero", "context_only_r_c", "old_authority_code_drift_fail_closed", "gate_pass"):
            if key in acceptance and acceptance.get(key) is not True:
                raise CompactStageADevelopmentError("topic_guided_review_invalid")

    privacy = value.get("privacy_lint") if isinstance(value.get("privacy_lint"), Mapping) else {}
    if privacy:
        if privacy.get("body_key_hits") not in (None, []) or privacy.get("hint_body_key_hits") not in (None, []):
            raise CompactStageADevelopmentError("topic_guided_review_invalid")
        for key in ("synthetic_marker_persisted", "raw_message_body_persisted"):
            if privacy.get(key) is True:
                raise CompactStageADevelopmentError("topic_guided_review_invalid")

    rows = _topic_guided_review_page_rows(value)
    hashes: Dict[str, str] = {}
    lineage = value.get("input_lineage") if isinstance(value.get("input_lineage"), Mapping) else {}
    for raw_key, raw_value in lineage.items():
        key = str(raw_key)
        if key.casefold().endswith("_sha256") and raw_value not in (None, ""):
            try:
                hashes[key] = _authority_projection_sha256(raw_value, "topic_guided_review_hash_invalid")
            except CompactStageADevelopmentError as exc:
                raise CompactStageADevelopmentError("topic_guided_review_hash_invalid") from exc
    return rows, hashes


def _synthetic_topic_guided_review(
    selected: Sequence[Tuple[Mapping[str, Any], Tuple[str, ...]]],
    *,
    requests: Sequence[Mapping[str, Any]] = (),
    selection_sha256: str,
) -> Mapping[str, Any]:
    """Build an in-memory body-free topic-quality witness for synthetic tests."""

    if len(selected) != TOPIC_GUIDED_MAX_SELECTED_PAGES:
        raise CompactStageADevelopmentError("topic_guided_page_drift")
    page_rows: List[Mapping[str, Any]] = []
    primary_counts: List[int] = []
    g_counts: List[int] = []
    context_counts: List[int] = []
    for index, (page, _categories) in enumerate(selected, 1):
        request = requests[index - 1] if index <= len(requests) and isinstance(requests[index - 1], Mapping) else {}
        primary_count = sum(1 for row in request.get("h", ()) if isinstance(row, Mapping) and row.get("k") == "m" and row.get("r") == "p")
        context_count = sum(1 for row in request.get("h", ()) if isinstance(row, Mapping) and row.get("k") == "m" and row.get("r") == "c")
        hints = request.get(GROUPING_HINTS_FIELD, ())
        primary_counts.append(primary_count)
        context_counts.append(context_count)
        g_counts.append(len(hints) if isinstance(hints, list) else 0)
        page_rows.append(
            {
                "rank": index,
                "opaque_page_ref": str(page.get("page_id") or "synthetic-page-%d" % index),
                "primary_rows": primary_count,
                "g_count": len(hints) if isinstance(hints, list) else 0,
                "g_le_16": len(hints) <= MAX_GENERATED_TOPIC_GROUPING_HINTS if isinstance(hints, list) else True,
                "g_exact_shape": True,
                "g_body_key_hits": [],
            }
        )
    return {
        "schema": TOPIC_GUIDED_REVIEW_SCHEMA,
        "review_status": TOPIC_GUIDED_REVIEW_STATUS,
        "gate_pass": True,
        "review_mode": "development_only_offline_protocol_build",
        "body_free": True,
        "identity_form": "opaque_only",
        "execution_boundaries": {
            "provider_called": False,
            "provider_call_count": 0,
            "authority_bound_runner_invoked": False,
            "authorization_ledger_opened": False,
            "frozen_semantics_read": False,
            "frozen_test_semantics_read": False,
            "gold_semantics_read": False,
            "development_input_read": True,
            "implementation_modified": False,
            "production_state_written": False,
            "diagnostic_provider_call_count": 0,
        },
        "input_lineage": {
            "selection_sha256": str(selection_sha256),
            "context_input_sha256": stable_hash({"synthetic_topic_guided_context": True}),
        },
        "selection_scope": {
            "selected_page_count": TOPIC_GUIDED_MAX_SELECTED_PAGES,
            "same_five_page_slice": True,
            "development_only": True,
            "candidate_only": True,
            "not_canonical": True,
            "semantic_decision_pending": True,
            "single_scope": True,
            "scope_count": 1,
            "cross_scope_message_rows": 0,
            "opaque_page_refs": [str(row["opaque_page_ref"]) for row in page_rows],
        },
        "human_true_substantive_baseline": {
            "expected_counts": list(primary_counts),
            "observed_counts": list(primary_counts),
            "total": sum(primary_counts),
            "exact": True,
        },
        "five_page_offline_build": {
            "primary_counts": list(primary_counts),
            "context_counts": list(context_counts),
            "primary_exact_baseline": True,
            "all_http_le_1600": True,
            "all_output_le_400": True,
            "context_source_role": "c",
            "context_strictly_r_c": True,
            "context_fallback_used": False,
            "context_repair_or_coercion": False,
        },
        "pages": page_rows,
        "grouping_hint_review": {
            "real_page_max_g_rows": max(g_counts or [0]),
            "real_page_all_g_le_16": all(count <= MAX_GENERATED_TOPIC_GROUPING_HINTS for count in g_counts),
            "real_page_g_body_key_hits": [],
            "real_page_candidate_auxiliary_hints_only": True,
            "dense_synthetic_message_rows_preserved": True,
            "dense_synthetic_emitted_is_deterministic_prefix": True,
            "dense_synthetic_body_marker_in_g": False,
            "representative_witnesses_complete": True,
            "generated_hint_selector_does_not_delete_message_rows": True,
        },
        "authority_evidence": {
            "actual_media_mispromotion_total": 0,
            "actual_empty_authority_mispromotion_total": 0,
            "actual_placeholder_mispromotion_total": 0,
            "actual_unbound_cue_promoted_total": 0,
            "actual_unmarked_system_or_event_mispromotion_total": 0,
            "actual_forbidden_repromotion_total": 0,
        },
        "acceptance": {
            "all_http_le_1600": True,
            "all_output_le_400": True,
            "primary_exact_baseline": True,
            "g_le_16": True,
            "g_representative_witnesses": True,
            "g_does_not_delete_message_rows": True,
            "placeholder_direct_cue_context": True,
            "system_event_caption_narrow_exception": True,
            "media_mispromotion_zero": True,
            "empty_authority_mispromotion_zero": True,
            "unbound_promoted_zero": True,
            "context_only_r_c": True,
            "old_authority_code_drift_fail_closed": True,
            "gate_pass": True,
        },
        "privacy_lint": {
            "body_key_hits": [],
            "hint_body_key_hits": [],
            "synthetic_marker_persisted": False,
            "raw_message_body_persisted": False,
            "identity_value_hits": 0,
        },
    }


def _read_topic_guided_review_evidence(
    root: Optional[Union[str, Path]],
    *,
    selected: Sequence[Tuple[Mapping[str, Any], Tuple[str, ...]]],
    requests: Sequence[Mapping[str, Any]],
    source_selection_sha256: str,
    summary_override: Optional[Mapping[str, Any]] = None,
) -> Tuple[Mapping[str, Any], Dict[str, str]]:
    """Read the latest topic-quality gate without opening provider inputs."""

    synthetic = False
    if summary_override is not None:
        summary: Mapping[str, Any] = dict(summary_override)
        review_sha256 = stable_hash(summary)
        synthetic = True
    elif root is None:
        summary = _synthetic_topic_guided_review(
            selected,
            requests=requests,
            selection_sha256=source_selection_sha256,
        )
        review_sha256 = stable_hash(summary)
        synthetic = True
    else:
        review_root = _safe_path(root, "topic_guided_review_invalid")
        path = review_root if review_root.suffix.casefold() == ".json" else review_root / TOPIC_GUIDED_REVIEW_FILENAME
        if not path.is_file():
            raise CompactStageADevelopmentError("topic_guided_review_invalid")
        summary = _json_read(path, "topic_guided_review_invalid")
        review_sha256 = _file_sha256(path)
        review_sha256 = _authority_projection_sha256(review_sha256, "topic_guided_review_hash_missing")
    _rows, lineage_hashes = _validate_topic_guided_review(summary)
    lineage_hashes["topic_guided_review_page_refs_sha256"] = stable_hash(
        [str(row.get("opaque_page_ref") or row.get("page_ref") or "") for row in _rows]
    )
    lineage = summary.get("input_lineage") if isinstance(summary.get("input_lineage"), Mapping) else {}
    audited_selection = str(lineage.get("selection_sha256") or "").strip().lower()
    if audited_selection and source_selection_sha256 and audited_selection != str(source_selection_sha256).strip().lower():
        raise CompactStageADevelopmentError("topic_guided_page_drift")
    if source_selection_sha256:
        _authority_projection_sha256(source_selection_sha256, "topic_guided_review_hash_missing")
        lineage_hashes.setdefault("selection_sha256", str(source_selection_sha256).strip().lower())
    lineage_hashes["topic_guided_review_sha256"] = review_sha256
    lineage_hashes["topic_guided_guidance_sha256"] = _topic_guided_guidance_sha256()
    lineage_hashes["topic_guided_grouping_hints_policy_sha256"] = _topic_guided_grouping_hints_sha256()
    lineage_hashes["topic_guided_protocol_source_sha256"] = _topic_guided_protocol_source_sha256()
    if synthetic:
        lineage_hashes["topic_guided_review_synthetic_sha256"] = stable_hash(
            {"review": "synthetic_test_only", "selected_page_count": len(selected)}
        )
    return summary, lineage_hashes


def _topic_guided_request_stats(request: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate and fingerprint one request's structural ``g`` hints."""

    if not isinstance(request, Mapping):
        raise CompactStageADevelopmentError("topic_guided_grouping_hint_drift")
    if request.get("context_unique") != "global":
        raise CompactStageADevelopmentError("topic_guided_grouping_hint_drift")
    rows = request.get("h")
    if not isinstance(rows, Sequence):
        raise CompactStageADevelopmentError("topic_guided_grouping_hint_drift")
    try:
        wire_stats = measure_wire_size(request)
    except Exception as exc:
        raise CompactStageADevelopmentError("topic_guided_grouping_hint_drift") from exc
    if wire_stats.calibrated_input_proxy > CALIBRATED_INPUT_TOKEN_PROXY_LIMIT:
        raise CompactStageADevelopmentError("input_token_limit_exceeded")
    try:
        candidate_components = build_candidate_connected_components(request)
    except Exception as exc:
        raise CompactStageADevelopmentError("topic_guided_grouping_hint_drift") from exc
    aliases = {str(row.get("i")) for row in rows if isinstance(row, Mapping)}
    hints = request.get(GROUPING_HINTS_FIELD, [])
    if hints is None:
        hints = []
    if not isinstance(hints, list) or len(hints) > MAX_TOPIC_GROUPING_HINTS or len(hints) > MAX_GENERATED_TOPIC_GROUPING_HINTS:
        raise CompactStageADevelopmentError("topic_guided_grouping_hint_drift")
    normalized: List[Mapping[str, str]] = []
    relation_counts: Dict[str, int] = {}
    for hint in hints:
        if not isinstance(hint, Mapping) or set(hint) != set(TOPIC_HINT_KEYS):
            raise CompactStageADevelopmentError("topic_guided_grouping_hint_drift")
        a, b, relation = str(hint.get("a") or ""), str(hint.get("b") or ""), str(hint.get("r") or "")
        if not a or not b or a == b or a not in aliases or b not in aliases:
            raise CompactStageADevelopmentError("topic_guided_grouping_hint_drift")
        components = relation.split("+")
        if not components or len(components) > TOPIC_HINT_MAX_RELATIONS or any(item not in TOPIC_HINT_RELATIONS for item in components):
            raise CompactStageADevelopmentError("topic_guided_grouping_hint_drift")
        normalized.append({"a": a, "b": b, "r": relation})
        for component in components:
            relation_counts[component] = relation_counts.get(component, 0) + 1
    try:
        _body_free({"g": normalized})
    except CompactStageADevelopmentError as exc:
        raise CompactStageADevelopmentError("topic_guided_grouping_hint_drift") from exc
    return {
        "count": len(normalized),
        "relations": dict(sorted(relation_counts.items())),
        "sha256": stable_hash(normalized),
        "message_count": sum(1 for row in rows if isinstance(row, Mapping) and row.get("k") == "m"),
        "primary_count": sum(1 for row in rows if isinstance(row, Mapping) and row.get("k") == "m" and row.get("r") == "p"),
        "context_count": sum(1 for row in rows if isinstance(row, Mapping) and row.get("k") == "m" and row.get("r") == "c"),
        "proxy": int(wire_stats.http_token_proxy),
        "http_token_proxy": int(wire_stats.http_token_proxy),
        "calibrated_input_token_proxy": int(wire_stats.calibrated_input_proxy),
        "calibrated_proxy": int(wire_stats.calibrated_input_proxy),
        "token_calibration_version": TOKEN_CALIBRATION_VERSION,
        "calibrated_within_limit": bool(
            wire_stats.calibrated_input_proxy <= CALIBRATED_INPUT_TOKEN_PROXY_LIMIT
        ),
        "candidate_component_count": len(candidate_components.get("components", ())),
        "candidate_split_evidence_count": int(candidate_components.get("split_evidence_count", 0) or 0),
        "candidate_merge_prior": str(candidate_components.get("merge_prior", "")),
        "candidate_only": True,
        "model_decision_required": True,
        "final_topic_assignment": False,
    }


def _topic_guided_validate_requests(
    selected: Sequence[Tuple[Mapping[str, Any], Tuple[str, ...]]],
    requests: Sequence[Mapping[str, Any]],
    *,
    stratified_mode: bool,
) -> Dict[str, Any]:
    """Apply the topic-guided request/g-hint gate before opening a ledger."""

    if len(selected) != TOPIC_GUIDED_MAX_SELECTED_PAGES or len(requests) != len(selected):
        raise CompactStageADevelopmentError("topic_guided_page_drift")
    rows: List[Mapping[str, Any]] = []
    primary_counts: List[int] = []
    g_counts: List[int] = []
    for index, ((page, _categories), request) in enumerate(zip(selected, requests), 1):
        _validate_provider_primary_role(request)
        stats = _topic_guided_request_stats(request)
        rows.append(
            {
                "selection_rank": int(page.get("_selection_rank", index)),
                "page_ref": str(page.get("page_id") or ""),
                "request_sha256": stable_hash(request),
                "g_sha256": stats["sha256"],
                "g_count": stats["count"],
                "g_relations": stats["relations"],
                "message_count": stats["message_count"],
                "primary_count": stats["primary_count"],
                "context_count": stats["context_count"],
                "proxy": stats["proxy"],
                "calibrated_proxy": stats["calibrated_proxy"],
                "token_calibration_version": stats["token_calibration_version"],
                "candidate_component_count": stats["candidate_component_count"],
                "candidate_split_evidence_count": stats["candidate_split_evidence_count"],
                "context_kept": int((page.get("_context_selection_telemetry") or {}).get("context_kept", 0) or 0)
                if isinstance(page.get("_context_selection_telemetry"), Mapping)
                else 0,
                "context_deferred": int((page.get("_context_selection_telemetry") or {}).get("context_deferred", 0) or 0)
                if isinstance(page.get("_context_selection_telemetry"), Mapping)
                else 0,
            }
        )
        primary_counts.append(int(stats["primary_count"]))
        g_counts.append(int(stats["count"]))
    if stratified_mode and tuple(primary_counts) != TOPIC_GUIDED_EXPECTED_PRIMARY_COUNTS:
        raise CompactStageADevelopmentError("topic_guided_page_drift")
    if stratified_mode and tuple(g_counts) != TOPIC_GUIDED_EXPECTED_G_HINT_COUNTS:
        raise CompactStageADevelopmentError("topic_guided_grouping_hint_drift")
    if stratified_mode and any(
        tuple(sorted(str(key) for key in row["g_relations"])) != tuple(sorted(TOPIC_GUIDED_EXPECTED_G_RELATIONS))
        for row in rows
    ):
        raise CompactStageADevelopmentError("topic_guided_grouping_hint_drift")
    return {
        "rows": rows,
        "primary_counts": primary_counts,
        "g_counts": g_counts,
        "g_hint_binding_sha256": stable_hash(rows),
        "g_hint_policy_sha256": _topic_guided_grouping_hints_sha256(),
        "guidance_sha256": _topic_guided_guidance_sha256(),
        "token_calibration_version": TOKEN_CALIBRATION_VERSION,
        "proxy": [int(row["proxy"]) for row in rows],
        "calibrated_proxy": [int(row["calibrated_proxy"]) for row in rows],
        "context_kept": [int(row["context_kept"]) for row in rows],
        "context_deferred": [int(row["context_deferred"]) for row in rows],
        "all_calibrated_within_limit": all(
            int(row["calibrated_proxy"]) <= CALIBRATED_INPUT_TOKEN_PROXY_LIMIT for row in rows
        ),
    }


def offline_topic_guided_request_report(
    selected: Sequence[Tuple[Mapping[str, Any], Tuple[str, ...]]],
    requests: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Return body-free P/C/g and calibrated preflight metrics offline."""

    if len(selected) != len(requests):
        raise CompactStageADevelopmentError("topic_guided_page_drift")
    rows: List[Dict[str, Any]] = []
    for index, ((page, _categories), request) in enumerate(zip(selected, requests), 1):
        stats = _topic_guided_request_stats(request)
        selection = page.get("_context_selection_telemetry") if isinstance(page, Mapping) else {}
        selection = selection if isinstance(selection, Mapping) else {}
        role_projection = page.get("_role_projection_telemetry") if isinstance(page, Mapping) else {}
        role_projection = role_projection if isinstance(role_projection, Mapping) else {}
        recovery = context_recovery_snapshot(page) if isinstance(page, Mapping) else {}
        rows.append(
            {
                "selection_rank": int(page.get("_selection_rank", index)),
                "page_id": str(page.get("page_id", "")),
                "primary": int(stats["primary_count"]),
                "context": int(stats["context_count"]),
                "context_provider": int(stats["context_count"]),
                "context_source_total": int(selection.get("context_total", 0) or 0),
                "g": int(stats["count"]),
                "proxy": int(stats["proxy"]),
                "calibrated": int(stats["calibrated_proxy"]),
                "context_kept": int(selection.get("context_kept", selection.get("kept", 0)) or 0),
                "context_deferred": int(selection.get("context_deferred", selection.get("deferred", 0)) or 0),
                "context_coverage": float(selection.get("context_coverage", 1.0) or 0.0),
                "candidate_component_count": int(stats.get("candidate_component_count", 0) or 0),
                "candidate_split_evidence_count": int(stats.get("candidate_split_evidence_count", 0) or 0),
                # ``proxy`` is the raw wire estimate; expose the explicit
                # spelling as well so an offline report cannot be mistaken
                # for a calibrated estimate.
                "raw_proxy": int(stats["proxy"]),
                "calibration_version": str(stats["token_calibration_version"]),
                "recovery_available": bool(recovery.get("body_free") and recovery.get("version")),
                "recovery_complete_context_count": len(recovery.get("complete_context_handles", ())),
                "recovery_kept_context_count": len(recovery.get("kept_context_handles", ())),
                "recovery_deferred_context_count": len(recovery.get("deferred_context_handles", ())),
                "recovery_source_ref_count": int(recovery.get("source_ref_count", 0) or 0),
                "recovery_snapshot_sha256": str(recovery.get("snapshot_sha256", "")),
                "media_mispromotion_count": int(role_projection.get("media_mispromotion_count", 0) or 0),
                "empty_authority_mispromotion_count": int(role_projection.get("empty_authority_mispromotion_count", 0) or 0),
                "placeholder_mispromotion_count": int(role_projection.get("placeholder_mispromotion_count", 0) or 0),
                "reaction_mispromotion_count": int(role_projection.get("reaction_mispromotion_count", 0) or 0),
                "system_or_event_mispromotion_count": int(role_projection.get("system_or_event_mispromotion_count", 0) or 0),
                "unbound_primary_promotion_count": int(role_projection.get("unbound_primary_promotion_count", 0) or 0),
                "barrier_mispromotion_zero": bool(role_projection.get("barrier_mispromotion_zero", True)),
                "body_free": True,
            }
        )
    barrier_totals = {
        key: sum(int(row.get(key, 0) or 0) for row in rows)
        for key in (
            "media_mispromotion_count",
            "empty_authority_mispromotion_count",
            "placeholder_mispromotion_count",
            "reaction_mispromotion_count",
            "system_or_event_mispromotion_count",
            "unbound_primary_promotion_count",
        )
    }
    return {
        "version": TOPIC_GUIDED_CONTEXT_SELECTION_VERSION,
        "calibration_version": TOKEN_CALIBRATION_VERSION,
        "rows": rows,
        "primary_total": sum(row["primary"] for row in rows),
        "context_total": sum(row["context"] for row in rows),
        "context_provider_total": sum(row["context_provider"] for row in rows),
        "context_source_total": sum(row["context_source_total"] for row in rows),
        "g_total": sum(row["g"] for row in rows),
        "context_kept_total": sum(row["context_kept"] for row in rows),
        "context_deferred_total": sum(row["context_deferred"] for row in rows),
        "proxy": [row["proxy"] for row in rows],
        "raw_proxy": [row["raw_proxy"] for row in rows],
        "calibrated": [row["calibrated"] for row in rows],
        "all_calibrated_within_limit": all(
            row["calibrated"] <= CALIBRATED_INPUT_TOKEN_PROXY_LIMIT for row in rows
        ),
        "barrier_mispromotion_counts": barrier_totals,
        "media_mispromotion_total": barrier_totals["media_mispromotion_count"],
        "empty_authority_mispromotion_total": barrier_totals["empty_authority_mispromotion_count"],
        "placeholder_mispromotion_total": barrier_totals["placeholder_mispromotion_count"],
        "reaction_mispromotion_total": barrier_totals["reaction_mispromotion_count"],
        "system_or_event_mispromotion_total": barrier_totals["system_or_event_mispromotion_count"],
        "unbound_primary_promotion_total": barrier_totals["unbound_primary_promotion_count"],
        "barrier_mispromotion_zero": not any(barrier_totals.values()),
        "recovery_available": all(bool(row["recovery_available"]) for row in rows),
        "recovery_complete_context_total": sum(row["recovery_complete_context_count"] for row in rows),
        "recovery_kept_context_total": sum(row["recovery_kept_context_count"] for row in rows),
        "recovery_deferred_context_total": sum(row["recovery_deferred_context_count"] for row in rows),
        "recovery_source_ref_total": sum(row["recovery_source_ref_count"] for row in rows),
        "body_free": True,
    }


topic_guided_offline_preflight_report = offline_topic_guided_request_report


def _topic_guided_input_binding_hash(
    manifest: Mapping[str, Any],
    hashes: Mapping[str, str],
    lineage: Mapping[str, Any],
    selected: Sequence[Tuple[Mapping[str, Any], Tuple[str, ...]]],
    *,
    authorization_id: str,
    adapter_metadata: Mapping[str, str],
    model: str,
    protocol: str,
    settings_sha256: str,
    scope: Mapping[str, str],
    authority_projection_hashes: Mapping[str, str],
    authority_projection_page_refs: Sequence[str],
    topic_guided_review_hashes: Mapping[str, str],
    request_stats: Mapping[str, Any],
) -> str:
    """Bind authority, topic guidance and g-hint preflight in one digest."""

    normalized_scope = _scope(scope)
    selected_rows = [
        {
            "selection_rank": int(page.get("_selection_rank", index + 1)),
            "page_id": str(page.get("page_id", "")),
            "root_id": str(page.get("root_id", "")),
            "source_handle": str(page.get("_source_handle", page.get("source_handle", ""))),
            "scope_handle": str(page.get("_scope_handle", page.get("scope_handle", ""))),
            "page_hash": str(page.get("page_hash", "")),
            "scope": _scope(page.get("scope")),
            "categories": list(categories),
        }
        for index, (page, categories) in enumerate(selected)
    ]
    return stable_hash(
        {
            "binding_schema": "compact_stage_a_topic_guided_authorization_v1",
            "authorization_id": authorization_id,
            "artifact_version": TOPIC_GUIDED_ARTIFACT_VERSION,
            "artifact_namespace": TOPIC_GUIDED_ARTIFACT_NAMESPACE,
            "source_artifact_version": manifest.get("artifact_version"),
            "source_hashes": dict(sorted((str(key), str(value)) for key, value in hashes.items())),
            "selection_sha256": str(lineage.get("selection_sha256", "")),
            "context_input_sha256": str(lineage.get("context_input_sha256", "")),
            "audit_summary_sha256": str(lineage.get("audit_summary_sha256", "")),
            "human_audit_sha256": str(lineage.get("human_audit_sha256", "")),
            "authority_projection_hashes": dict(sorted((str(key), str(value)) for key, value in authority_projection_hashes.items())),
            "authority_projection_page_refs": [str(item) for item in authority_projection_page_refs],
            "authority_bound_canonical_mapping_version": TOPIC_GUIDED_CANONICAL_MAPPING_VERSION,
            "authority_bound_canonical_mapping_sha256": TOPIC_GUIDED_CANONICAL_MAPPING_SHA256,
            "authority_bound_canonical_mapping": [dict(row) for row in TOPIC_GUIDED_CANONICAL_MAPPING],
            "topic_guided_review_hashes": dict(sorted((str(key), str(value)) for key, value in topic_guided_review_hashes.items())),
            "topic_guided_guidance_version": TOPIC_GUIDED_GUIDANCE_VERSION,
            "topic_guided_guidance_sha256": _topic_guided_guidance_sha256(),
            "topic_guided_grouping_hints_version": TOPIC_GUIDED_GROUPING_HINTS_VERSION,
            "topic_guided_grouping_hints_policy_sha256": _topic_guided_grouping_hints_sha256(),
            "topic_guided_g_hint_binding_sha256": str(request_stats.get("g_hint_binding_sha256", "")),
            "topic_guided_protocol_source_sha256": _topic_guided_protocol_source_sha256(),
            "topic_guided_code_sha256": str(lineage.get("topic_guided_code_sha256", "")),
            "topic_guided_prompt_sha256": stable_hash(TOPIC_GUIDED_SYSTEM_PROMPT),
            "topic_guided_prompt_version": TOPIC_GUIDED_PROMPT_VERSION,
            "topic_guided_taxonomy_version": TOPIC_GUIDED_TAXONOMY_VERSION,
            "selected": selected_rows,
            "request_stats": dict(request_stats),
            "adapter": dict(adapter_metadata),
            "runner_code_sha256": str(adapter_metadata.get("code_sha256", "")),
            "source_code_sha256": _code_hash_fingerprint(manifest.get("code_sha256", "")),
            "model": model,
            "model_sha256": stable_hash(model),
            "protocol": protocol,
            "protocol_sha256": stable_hash(protocol),
            "settings_sha256": settings_sha256,
            "scope": normalized_scope,
            "scope_sha256": stable_hash(normalized_scope),
            "limits": {
                "max_total_calls": TOPIC_GUIDED_MAX_TOTAL_CALLS,
                "max_provider_calls": TOPIC_GUIDED_MAX_PROVIDER_CALLS,
                "selected_page_limit": TOPIC_GUIDED_MAX_SELECTED_PAGES,
                "per_page_provider_call_limit": TOPIC_GUIDED_PER_PAGE_PROVIDER_CALL_LIMIT,
                "max_retries": TOPIC_GUIDED_MAX_RETRIES,
                "supplement_calls": TOPIC_GUIDED_SUPPLEMENT_CALLS,
                "supplement_pages": TOPIC_GUIDED_SUPPLEMENT_PAGES,
                "health_calls": TOPIC_GUIDED_HEALTH_CALLS,
                "sdk_calls": TOPIC_GUIDED_SDK_CALLS,
            },
            "response_format_mode": RESPONSE_FORMAT_MODE,
            "response_format_sent": False,
            "thinking_disabled": THINKING_DISABLED,
            "stage_b_authorized": False,
            "stage_c_authorized": False,
            "production_authorized": False,
            "frozen_read": False,
            "gold_loaded": False,
            "no_supplement": True,
        }
    )


def _topic_guided_contract(
    *,
    authorization_id: str,
    adapter_metadata: Mapping[str, str],
    model: str,
    protocol: str,
    settings_sha256: str,
    scope: Mapping[str, str],
    lineage: Mapping[str, Any],
    input_binding_sha256: str,
    selected: Sequence[Tuple[Mapping[str, Any], Tuple[str, ...]]],
    request_stats: Mapping[str, Any],
    source_code_sha256: str = "",
) -> Dict[str, Any]:
    """Return the immutable body-free topic-guided authorization contract."""

    normalized_scope = _scope(scope)
    selected_rows = [
        {
            "selection_rank": int(page.get("_selection_rank", index + 1)),
            "page_ref": str(page.get("page_id", "")),
            "root_ref": str(page.get("root_id", "")),
            "source_ref": str(page.get("_source_handle", page.get("source_handle", ""))),
            "scope_ref": str(page.get("_scope_handle", page.get("scope_handle", ""))),
            "page_hash": str(page.get("page_hash", "")),
        }
        for index, (page, _categories) in enumerate(selected)
    ]
    authority_hashes = {
        str(key): str(value)
        for key, value in (lineage.get("authority_projection_hashes", {}) or {}).items()
    }
    review_hashes = {
        str(key): str(value)
        for key, value in (lineage.get("topic_guided_review_hashes", {}) or {}).items()
    }
    return {
        "binding_schema": "compact_stage_a_topic_guided_authorization_v1",
        "authorization_id": authorization_id,
        "artifact_version": TOPIC_GUIDED_ARTIFACT_VERSION,
        "artifact_namespace": TOPIC_GUIDED_ARTIFACT_NAMESPACE,
        "topic_guided_authorization": True,
        "topic_guided": True,
        "authority_bound_projection": True,
        "adapter": str(adapter_metadata.get("adapter", "unconfigured")),
        "adapter_source": str(adapter_metadata.get("adapter_source", SOURCE_ID)),
        "adapter_sha256": str(adapter_metadata.get("adapter_sha256", "")),
        "adapter_code_sha256": str(adapter_metadata.get("adapter_code_sha256", "")),
        "code_sha256": str(adapter_metadata.get("code_sha256", "")),
        "source_code_sha256": str(source_code_sha256 or ""),
        "topic_guided_code_sha256": str(lineage.get("topic_guided_code_sha256", "")),
        "topic_guided_prompt_version": TOPIC_GUIDED_PROMPT_VERSION,
        "topic_guided_prompt_sha256": stable_hash(TOPIC_GUIDED_SYSTEM_PROMPT),
        "topic_guided_guidance_version": TOPIC_GUIDED_GUIDANCE_VERSION,
        "topic_guided_guidance_sha256": str(lineage.get("topic_guided_guidance_sha256", "")),
        "topic_guided_grouping_hints_version": TOPIC_GUIDED_GROUPING_HINTS_VERSION,
        "topic_guided_grouping_hints_policy_sha256": str(lineage.get("topic_guided_grouping_hints_policy_sha256", "")),
        "topic_guided_g_hint_binding_sha256": str(lineage.get("topic_guided_g_hint_binding_sha256", "")),
        "topic_guided_protocol_source_sha256": str(lineage.get("topic_guided_protocol_source_sha256", "")),
        "topic_guided_taxonomy_version": TOPIC_GUIDED_TAXONOMY_VERSION,
        "topic_guided_review_hashes": review_hashes,
        "topic_guided_review_sha256": str(review_hashes.get("topic_guided_review_sha256", "")),
        "protocol": protocol,
        "protocol_sha256": stable_hash(protocol),
        "protocol_source_sha256": str(lineage.get("topic_guided_protocol_source_sha256", "")),
        "settings_sha256": settings_sha256,
        "model": model,
        "model_sha256": stable_hash(model),
        "scope": normalized_scope,
        "scope_sha256": stable_hash(normalized_scope),
        "selection_sha256": str(lineage.get("selection_sha256", "")),
        "context_input_sha256": str(lineage.get("context_input_sha256", "")),
        "audit_summary_sha256": str(lineage.get("audit_summary_sha256", "")),
        "human_audit_sha256": str(lineage.get("human_audit_sha256", "")),
        "authority_projection_hashes": authority_hashes,
        "authority_projection_page_refs": list(lineage.get("authority_projection_page_refs", ()) or ()),
        "authority_projection_scope_ref": str(lineage.get("authority_projection_scope_ref", "")),
        "authority_bound_canonical_mapping_version": TOPIC_GUIDED_CANONICAL_MAPPING_VERSION,
        "authority_bound_canonical_mapping_sha256": TOPIC_GUIDED_CANONICAL_MAPPING_SHA256,
        "authority_bound_canonical_mapping": [dict(row) for row in TOPIC_GUIDED_CANONICAL_MAPPING],
        "selected_page_count": TOPIC_GUIDED_MAX_SELECTED_PAGES,
        "selected_page_limit": TOPIC_GUIDED_MAX_SELECTED_PAGES,
        "selected_page_refs": [row["page_ref"] for row in selected_rows],
        "selected_root_refs": [row["root_ref"] for row in selected_rows],
        "selected_source_refs": [row["source_ref"] for row in selected_rows],
        "max_total_calls": TOPIC_GUIDED_MAX_TOTAL_CALLS,
        "max_provider_calls": TOPIC_GUIDED_MAX_PROVIDER_CALLS,
        "per_page_provider_call_limit": TOPIC_GUIDED_PER_PAGE_PROVIDER_CALL_LIMIT,
        "max_retries": TOPIC_GUIDED_MAX_RETRIES,
        "supplement_calls": TOPIC_GUIDED_SUPPLEMENT_CALLS,
        "supplement_pages": TOPIC_GUIDED_SUPPLEMENT_PAGES,
        "health_calls": TOPIC_GUIDED_HEALTH_CALLS,
        "sdk_calls": TOPIC_GUIDED_SDK_CALLS,
        "request_stats": dict(request_stats),
        "response_format_mode": RESPONSE_FORMAT_MODE,
        "response_format_sent": False,
        "thinking_disabled": THINKING_DISABLED,
        "no_supplement": True,
        "stage_b_authorized": False,
        "stage_c_authorized": False,
        "production_authorized": False,
        "frozen_read": False,
        "gold_loaded": False,
        "input_binding_sha256": input_binding_sha256,
        "body_free": True,
    }


def _topic_guided_validate_frozen_boundary(
    *,
    stratified_mode: bool,
    lineage: Mapping[str, Any],
    authority_projection_hashes: Mapping[str, str],
    topic_guided_review_hashes: Mapping[str, str],
    settings_sha256: str,
    scope: Mapping[str, str],
    topic_guided_code_sha256: str,
    request_stats: Mapping[str, Any],
    input_hashes: Mapping[str, str],
) -> None:
    """Fail closed on any topic-guided drift before ledger creation."""

    # Keep the budget and execution-mode policy itself inside the frozen
    # boundary.  These values are otherwise copied into the ledger/artifact
    # after this check, so a runtime/source drift could widen the budget while
    # leaving every input hash unchanged.  The topic-guided authorization is
    # deliberately exact: five pages, one attempt per page, no retries,
    # supplements, health, or SDK calls, and the compact wire's omitted
    # response-format marker with thinking disabled.
    if (
        TOPIC_GUIDED_MAX_SELECTED_PAGES != 5
        or TOPIC_GUIDED_MAX_TOTAL_CALLS != 5
        or TOPIC_GUIDED_MAX_PROVIDER_CALLS != 5
        or TOPIC_GUIDED_PER_PAGE_PROVIDER_CALL_LIMIT != 1
        or TOPIC_GUIDED_MAX_RETRIES != 0
        or TOPIC_GUIDED_SUPPLEMENT_CALLS != 0
        or TOPIC_GUIDED_SUPPLEMENT_PAGES != 0
        or TOPIC_GUIDED_HEALTH_CALLS != 0
        or TOPIC_GUIDED_SDK_CALLS != 0
        or RESPONSE_FORMAT_MODE != "omitted"
        or THINKING_DISABLED is not True
    ):
        raise CompactStageADevelopmentError("topic_guided_protocol_drift")
    if _authority_bound_canonical_mapping_hash() != TOPIC_GUIDED_CANONICAL_MAPPING_SHA256:
        raise CompactStageADevelopmentError("topic_guided_protocol_drift")
    if str(topic_guided_code_sha256) != TOPIC_GUIDED_CODE_SHA256:
        raise CompactStageADevelopmentError("topic_guided_protocol_drift")
    if stable_hash(TOPIC_GUIDED_SYSTEM_PROMPT) != TOPIC_GUIDED_PROMPT_SHA256:
        raise CompactStageADevelopmentError("topic_guided_guidance_drift")
    if _topic_guided_guidance_sha256() != TOPIC_GUIDED_GUIDANCE_SHA256:
        raise CompactStageADevelopmentError("topic_guided_guidance_drift")
    if _topic_guided_grouping_hints_sha256() != TOPIC_GUIDED_GROUPING_HINTS_SHA256:
        raise CompactStageADevelopmentError("topic_guided_grouping_hint_drift")
    if _topic_guided_protocol_source_sha256() != TOPIC_GUIDED_PROTOCOL_SOURCE_SHA256:
        raise CompactStageADevelopmentError("topic_guided_protocol_drift")
    try:
        normalized_scope = _scope(scope)
    except CompactStageADevelopmentError as exc:
        raise CompactStageADevelopmentError("topic_guided_scope_drift") from exc
    if normalized_scope != dict(TOPIC_GUIDED_SCOPE) or stable_hash(normalized_scope) != TOPIC_GUIDED_SCOPE_SHA256:
        raise CompactStageADevelopmentError("topic_guided_scope_drift")
    if str(settings_sha256).strip().lower() != TOPIC_GUIDED_SETTINGS_SHA256:
        raise CompactStageADevelopmentError("topic_guided_review_hash_mismatch")

    source_names = (
        ("selection_sha256", "authority_projection_selection_sha256"),
        ("context_input_sha256", "authority_projection_context_sha256"),
        ("audit_summary_sha256", "authority_projection_audit_summary_sha256"),
        ("human_audit_sha256", "authority_projection_human_audit_sha256"),
    )
    for source_name, projection_name in source_names:
        source_digest = str(lineage.get(source_name) or "").strip().lower()
        projection_digest = str(authority_projection_hashes.get(projection_name) or "").strip().lower()
        if not source_digest or not projection_digest:
            raise CompactStageADevelopmentError("topic_guided_review_hash_missing")
        _authority_projection_sha256(source_digest, "topic_guided_review_hash_missing")
        _authority_projection_sha256(projection_digest, "topic_guided_review_hash_missing")
        if source_digest != projection_digest:
            raise CompactStageADevelopmentError("topic_guided_review_hash_mismatch")

    if not str(topic_guided_review_hashes.get("topic_guided_review_sha256") or ""):
        raise CompactStageADevelopmentError("topic_guided_review_hash_missing")
    _authority_projection_sha256(topic_guided_review_hashes["topic_guided_review_sha256"], "topic_guided_review_hash_missing")
    if not str(topic_guided_review_hashes.get("topic_guided_guidance_sha256") or ""):
        raise CompactStageADevelopmentError("topic_guided_review_hash_missing")
    if str(topic_guided_review_hashes.get("topic_guided_guidance_sha256")) != TOPIC_GUIDED_GUIDANCE_SHA256:
        raise CompactStageADevelopmentError("topic_guided_guidance_drift")
    if str(topic_guided_review_hashes.get("topic_guided_grouping_hints_policy_sha256")) != TOPIC_GUIDED_GROUPING_HINTS_SHA256:
        raise CompactStageADevelopmentError("topic_guided_grouping_hint_drift")

    if str(request_stats.get("g_hint_policy_sha256")) != TOPIC_GUIDED_GROUPING_HINTS_SHA256 or str(request_stats.get("guidance_sha256")) != TOPIC_GUIDED_GUIDANCE_SHA256:
        raise CompactStageADevelopmentError("topic_guided_grouping_hint_drift")
    if stratified_mode:
        expected_lineage = (
            ("selection_sha256", TOPIC_GUIDED_SELECTION_SHA256),
            ("context_input_sha256", TOPIC_GUIDED_CONTEXT_INPUT_SHA256),
            ("audit_summary_sha256", TOPIC_GUIDED_AUTHORITY_AUDIT_SUMMARY_SHA256),
            ("human_audit_sha256", TOPIC_GUIDED_AUTHORITY_HUMAN_AUDIT_SHA256),
        )
        for name, expected in expected_lineage:
            if str(lineage.get(name) or "").strip().lower() != expected:
                raise CompactStageADevelopmentError("topic_guided_review_hash_mismatch")
        for projection_name, expected in (
            ("authority_projection_selection_sha256", TOPIC_GUIDED_SELECTION_SHA256),
            ("authority_projection_context_sha256", TOPIC_GUIDED_CONTEXT_INPUT_SHA256),
            ("authority_projection_audit_summary_sha256", TOPIC_GUIDED_AUTHORITY_AUDIT_SUMMARY_SHA256),
            ("authority_projection_human_audit_sha256", TOPIC_GUIDED_AUTHORITY_HUMAN_AUDIT_SHA256),
        ):
            if str(authority_projection_hashes.get(projection_name) or "").strip().lower() != expected:
                raise CompactStageADevelopmentError("topic_guided_review_hash_mismatch")
        if str(topic_guided_review_hashes.get("topic_guided_review_sha256")) != TOPIC_GUIDED_REVIEW_SHA256:
            raise CompactStageADevelopmentError("topic_guided_review_hash_mismatch")
        if str(topic_guided_review_hashes.get("topic_guided_review_page_refs_sha256") or "") != stable_hash(list(TOPIC_GUIDED_REVIEW_PAGE_REFS)):
            raise CompactStageADevelopmentError("topic_guided_page_drift")
        for name, expected in (
            ("manifest_sha256", TOPIC_GUIDED_MANIFEST_SHA256),
            ("development_source_sha256", TOPIC_GUIDED_DEVELOPMENT_SOURCE_SHA256),
            ("protocol_source_sha256", TOPIC_GUIDED_PROTOCOL_SOURCE_SHA256),
            ("previous_review_sha256", TOPIC_GUIDED_PREVIOUS_REVIEW_SHA256),
        ):
            if str(topic_guided_review_hashes.get(name) or "").strip().lower() != expected:
                raise CompactStageADevelopmentError("topic_guided_review_hash_mismatch")
        if str(input_hashes.get("source_manifest_sha256") or input_hashes.get("manifest_sha256") or "").strip().lower() != TOPIC_GUIDED_MANIFEST_SHA256:
            raise CompactStageADevelopmentError("topic_guided_review_hash_mismatch")
        if str(input_hashes.get("context_manifest_sha256") or "").strip().lower() not in {"", TOPIC_GUIDED_CONTEXT_MANIFEST_SHA256}:
            raise CompactStageADevelopmentError("topic_guided_review_hash_mismatch")


def _validate_provider_primary_role(request: Mapping[str, Any]) -> None:
    """Verify only semantic message cues enter provider-facing primary."""

    rows = request.get("h")
    if not isinstance(rows, Sequence):
        raise CompactStageADevelopmentError("provider_primary_role_invalid")
    primary = [row for row in rows if isinstance(row, Mapping) and row.get("k") == "m" and row.get("r") == "p"]
    if not primary:
        raise CompactStageADevelopmentError("page_no_primary_message")
    if any(not _semantic_cue_is_eligible(row.get("x", "")) for row in primary):
        raise CompactStageADevelopmentError("primary_alias_not_semantic_eligible")
    if any(row.get("k") == "c" and not str(row.get("h", "")).split("|", 2)[1:2] == ["candidate"] for row in rows if isinstance(row, Mapping)):
        raise CompactStageADevelopmentError("provider_primary_role_invalid")


def run_compact_stage_a_development_pilot_v3(
    input_directory: Union[str, Path, Sequence[Mapping[str, Any]]] = DEFAULT_INPUT_DIRECTORY,
    output_directory: Union[str, Path] = DEFAULT_ARTIFACT_DIRECTORY,
    *,
    model: Optional[Any] = None,
    provider: Optional[Any] = None,
    pages: Optional[Sequence[Mapping[str, Any]]] = None,
    store: Optional[Mapping[str, Any]] = None,
    manifest: Optional[Mapping[str, Any]] = None,
    context_input_directory: Union[str, Path] = DEFAULT_CONTEXT_INPUT_DIRECTORY,
    context_directory: Optional[Union[str, Path]] = None,
    audit_directory: Union[str, Path] = DEFAULT_AUDIT_DIRECTORY,
    audit_summary: Optional[Mapping[str, Any]] = None,
    human_audit: Optional[Sequence[Mapping[str, Any]]] = None,
    authority_root: Union[str, Path] = DEFAULT_AUTHORITY_ROOT,
    settings_path: Union[str, Path] = DEFAULT_SETTINGS_PATH,
    settings_sha256: Optional[str] = None,
    health_artifact_directory: Optional[Union[str, Path]] = None,
    health_verified: Optional[bool] = None,
    cache: Optional[MutableMapping[str, Mapping[str, Any]]] = None,
    authorization_id: str = AUTHORIZATION_ID,
    guard_validation_subset: Optional[Sequence[Mapping[str, Any]]] = None,
    guard_subset: Optional[Sequence[Mapping[str, Any]]] = None,
    audit_subset: Optional[Sequence[Mapping[str, Any]]] = None,
    guard_audit_directory: Optional[Union[str, Path]] = None,
    guard_audit_summary: Optional[Mapping[str, Any]] = None,
    guard_human_audit: Optional[Sequence[Mapping[str, Any]]] = None,
    guards_replay: Optional[Mapping[str, Any]] = None,
    guard_replay: Optional[Mapping[str, Any]] = None,
    # Authority-bound projection evidence is metadata-only.  The aliases are
    # intentionally explicit so callers cannot smuggle a body-bearing source
    # packet through the generic audit arguments.
    authority_projection_audit_directory: Optional[Union[str, Path]] = None,
    authority_bound_audit_directory: Optional[Union[str, Path]] = None,
    authority_projection_audit_summary: Optional[Mapping[str, Any]] = None,
    authority_bound_audit_summary: Optional[Mapping[str, Any]] = None,
    authority_projection_summary: Optional[Mapping[str, Any]] = None,
    authority_projection_audit: Optional[Mapping[str, Any]] = None,
    authority_bound_audit: Optional[Mapping[str, Any]] = None,
    authority_projection_hashes: Optional[Mapping[str, Any]] = None,
    authority_bound_hashes: Optional[Mapping[str, Any]] = None,
    # Topic-guided offline gate evidence is metadata-only.  These aliases
    # mirror the authority-bound surface while keeping the new one-time
    # authorization namespace explicit; hashes are assertions only and are
    # always recomputed from the selected source/evidence at runtime.
    topic_guided_review_directory: Optional[Union[str, Path]] = None,
    topic_guided_audit_directory: Optional[Union[str, Path]] = None,
    topic_guided_review_summary: Optional[Mapping[str, Any]] = None,
    topic_guided_audit_summary: Optional[Mapping[str, Any]] = None,
    topic_guided_summary: Optional[Mapping[str, Any]] = None,
    topic_guided_hashes: Optional[Mapping[str, Any]] = None,
    topic_guided_review_hashes: Optional[Mapping[str, Any]] = None,
    topic_guided_guidance_sha256: Optional[str] = None,
    topic_guided_grouping_hints_sha256: Optional[str] = None,
    topic_guided_g_hint_binding_sha256: Optional[str] = None,
) -> CompactStageADevelopmentResult:
    """Prepare/run one bounded Stage-A v3 pilot.

    ``model``/``provider`` is intentionally injected.  When neither is
    supplied the function writes a blocked body-free artifact without making
    any network call.  A path named by ``INPUT_ARTIFACT_VERSION`` uses the
    audited global five-page stratified plan, the current K2 packet bodies in
    memory, and the current Stage-A-only audit sidecar.  Legacy K10 path and
    synthetic inputs remain available solely for offline regression tests.
    """

    if type(authorization_id) is not str or (
        authorization_id not in ALLOWED_AUTHORIZATION_IDS
        and authorization_id != TOPIC_GUIDED_AUTHORIZATION_ID
    ):
        # Authorization is an allow-list, not a caller-supplied namespace.  Do
        # this before reading input or constructing a model so an unknown id
        # cannot create a budget or reach a provider path.
        raise CompactStageADevelopmentError("authorization_binding_mismatch")
    adapter_fix_authorization = authorization_id == ADAPTER_FIX_AUTHORIZATION_ID
    guard_validation = authorization_id == GUARD_VALIDATION_AUTHORIZATION_ID
    guarded_authorization = authorization_id == GUARDED_AUTHORIZATION_ID
    authority_bound_authorization = authorization_id == AUTHORITY_BOUND_AUTHORIZATION_ID
    topic_guided_authorization = authorization_id == TOPIC_GUIDED_AUTHORIZATION_ID
    authority_alias_values = (
        authority_projection_audit_directory,
        authority_bound_audit_directory,
        authority_projection_audit_summary,
        authority_bound_audit_summary,
        authority_projection_summary,
        authority_projection_audit,
        authority_bound_audit,
        authority_projection_hashes,
        authority_bound_hashes,
    )
    if not (authority_bound_authorization or topic_guided_authorization) and any(value is not None for value in authority_alias_values):
        raise CompactStageADevelopmentError("authorization_binding_mismatch")
    topic_alias_values = (
        topic_guided_review_directory,
        topic_guided_audit_directory,
        topic_guided_review_summary,
        topic_guided_audit_summary,
        topic_guided_summary,
        topic_guided_hashes,
        topic_guided_review_hashes,
        topic_guided_guidance_sha256,
        topic_guided_grouping_hints_sha256,
        topic_guided_g_hint_binding_sha256,
    )
    if not topic_guided_authorization and any(value is not None for value in topic_alias_values):
        raise CompactStageADevelopmentError("authorization_binding_mismatch")
    if authority_bound_authorization or topic_guided_authorization:
        # Distinct aliases are accepted for compatibility, but two different
        # values must never silently choose one projection source.
        if (
            authority_projection_audit_summary is not None
            and authority_bound_audit_summary is not None
            and authority_projection_audit_summary != authority_bound_audit_summary
        ) or (
            authority_projection_summary is not None
            and authority_projection_audit_summary is not None
            and authority_projection_summary != authority_projection_audit_summary
        ) or (
            authority_projection_audit is not None
            and authority_bound_audit is not None
            and authority_projection_audit != authority_bound_audit
        ) or (
            authority_projection_hashes is not None
            and authority_bound_hashes is not None
            and authority_projection_hashes != authority_bound_hashes
        ):
            raise CompactStageADevelopmentError("authority_projection_hash_mismatch")
        if authority_projection_audit_directory is not None and authority_bound_audit_directory is not None:
            try:
                if _safe_path(authority_projection_audit_directory, "authority_projection_invalid") != _safe_path(authority_bound_audit_directory, "authority_projection_invalid"):
                    raise CompactStageADevelopmentError("authority_projection_hash_mismatch")
            except CompactStageADevelopmentError:
                raise
    if topic_guided_authorization:
        if (
            topic_guided_review_summary is not None
            and topic_guided_audit_summary is not None
            and topic_guided_review_summary != topic_guided_audit_summary
        ) or (
            topic_guided_summary is not None
            and topic_guided_review_summary is not None
            and topic_guided_summary != topic_guided_review_summary
        ) or (
            topic_guided_summary is not None
            and topic_guided_audit_summary is not None
            and topic_guided_summary != topic_guided_audit_summary
        ) or (
            topic_guided_hashes is not None
            and topic_guided_review_hashes is not None
            and topic_guided_hashes != topic_guided_review_hashes
        ):
            raise CompactStageADevelopmentError("topic_guided_review_hash_mismatch")
        if topic_guided_review_directory is not None and topic_guided_audit_directory is not None:
            if _safe_path(topic_guided_review_directory, "topic_guided_review_invalid") != _safe_path(topic_guided_audit_directory, "topic_guided_review_invalid"):
                raise CompactStageADevelopmentError("topic_guided_review_hash_mismatch")
    if (authority_bound_authorization or topic_guided_authorization) and any(
        value is not None
        for value in (
            guard_validation_subset,
            guard_subset,
            audit_subset,
            guard_audit_directory,
            guard_audit_summary,
            guard_human_audit,
            guards_replay,
            guard_replay,
        )
    ):
        raise CompactStageADevelopmentError("guard_subset_invalid")
    if not guard_validation and any(
        value is not None
        for value in (
            guard_validation_subset,
            guard_subset,
            audit_subset,
            guard_audit_directory,
            guard_audit_summary,
            guard_human_audit,
            guards_replay,
            guard_replay,
        )
    ):
        raise CompactStageADevelopmentError("guard_subset_invalid")
    supplied_subsets = [
        value
        for value in (guard_validation_subset, guard_subset, audit_subset)
        if value is not None
    ]
    if guard_validation:
        if supplied_subsets:
            subset_hashes = {_guard_subset_hash(_guard_subset_rows(value)) for value in supplied_subsets}
            if len(subset_hashes) != 1:
                raise CompactStageADevelopmentError("guard_subset_invalid")
            guard_subset_rows = _guard_subset_rows(supplied_subsets[0])
        else:
            guard_subset_rows = _guard_subset_rows(GUARD_VALIDATION_SUBSET)
        if guards_replay is not None and guard_replay is not None and guards_replay != guard_replay:
            raise CompactStageADevelopmentError("guard_audit_invalid")
        if guards_replay is None:
            guards_replay = guard_replay
    else:
        guard_subset_rows = ()
    if topic_guided_authorization:
        selected_page_limit = TOPIC_GUIDED_MAX_SELECTED_PAGES
        provider_call_limit = TOPIC_GUIDED_MAX_TOTAL_CALLS
        per_page_provider_call_limit = TOPIC_GUIDED_PER_PAGE_PROVIDER_CALL_LIMIT
        max_retries = TOPIC_GUIDED_MAX_RETRIES
        sdk_calls = TOPIC_GUIDED_SDK_CALLS
        artifact_version = TOPIC_GUIDED_ARTIFACT_VERSION
        artifact_namespace = TOPIC_GUIDED_ARTIFACT_NAMESPACE
        system_prompt = TOPIC_GUIDED_SYSTEM_PROMPT
        guard_code_sha256 = _topic_guided_code_sha256()
    elif authority_bound_authorization:
        selected_page_limit = AUTHORITY_BOUND_MAX_SELECTED_PAGES
        provider_call_limit = AUTHORITY_BOUND_MAX_TOTAL_CALLS
        per_page_provider_call_limit = AUTHORITY_BOUND_PER_PAGE_PROVIDER_CALL_LIMIT
        max_retries = AUTHORITY_BOUND_MAX_RETRIES
        sdk_calls = AUTHORITY_BOUND_SDK_CALLS
        artifact_version = AUTHORITY_BOUND_ARTIFACT_VERSION
        artifact_namespace = AUTHORITY_BOUND_ARTIFACT_NAMESPACE
        system_prompt = AUTHORITY_BOUND_SYSTEM_PROMPT
        guard_code_sha256 = _authority_bound_code_sha256()
    elif guard_validation:
        selected_page_limit = GUARD_VALIDATION_MAX_SELECTED_PAGES
        provider_call_limit = GUARD_VALIDATION_MAX_PROVIDER_CALLS
        per_page_provider_call_limit = GUARD_VALIDATION_PER_PAGE_PROVIDER_CALL_LIMIT
        max_retries = GUARD_VALIDATION_MAX_RETRIES
        sdk_calls = GUARD_VALIDATION_SDK_CALLS
        artifact_version = GUARD_VALIDATION_ARTIFACT_VERSION
        artifact_namespace = GUARD_VALIDATION_ARTIFACT_NAMESPACE
        system_prompt = GUARD_VALIDATION_SYSTEM_PROMPT
        guard_code_sha256 = _guard_validation_code_sha256()
    elif guarded_authorization:
        selected_page_limit = GUARDED_MAX_SELECTED_PAGES
        provider_call_limit = GUARDED_MAX_PROVIDER_CALLS
        per_page_provider_call_limit = GUARDED_PER_PAGE_PROVIDER_CALL_LIMIT
        max_retries = GUARDED_MAX_RETRIES
        sdk_calls = GUARDED_SDK_CALLS
        artifact_version = GUARDED_ARTIFACT_VERSION
        artifact_namespace = GUARDED_ARTIFACT_NAMESPACE
        system_prompt = GUARDED_SYSTEM_PROMPT
        guard_code_sha256 = _guarded_code_sha256()
    else:
        selected_page_limit = MAX_SELECTED_PAGES
        provider_call_limit = MAX_PROVIDER_CALLS
        per_page_provider_call_limit = PER_PAGE_PROVIDER_CALL_LIMIT
        max_retries = MAX_RETRIES
        sdk_calls = 0
        artifact_version = ARTIFACT_VERSION
        artifact_namespace = ARTIFACT_NAMESPACE
        system_prompt = SYSTEM_PROMPT
        guard_code_sha256 = ""
    try:
        output_is_default_artifact = _safe_path(output_directory, "input_artifact_invalid") == _safe_path(
            DEFAULT_ARTIFACT_DIRECTORY, "input_artifact_invalid"
        )
    except CompactStageADevelopmentError:
        output_is_default_artifact = False
    if topic_guided_authorization and output_is_default_artifact:
        output_directory = DEFAULT_TOPIC_GUIDED_ARTIFACT_DIRECTORY
    elif authority_bound_authorization and output_is_default_artifact:
        output_directory = DEFAULT_AUTHORITY_BOUND_ARTIFACT_DIRECTORY
    elif guard_validation and output_is_default_artifact:
        output_directory = DEFAULT_GUARD_VALIDATION_ARTIFACT_DIRECTORY
    elif guarded_authorization and output_is_default_artifact:
        output_directory = DEFAULT_GUARDED_ARTIFACT_DIRECTORY
    output_root = _safe_path(output_directory, "input_artifact_invalid")
    if topic_guided_authorization or authority_bound_authorization:
        # Never place the new contract under one of the historical artifact
        # namespaces, even when that directory has not been created yet.
        legacy_artifact_roots = (
            DEFAULT_ARTIFACT_DIRECTORY,
            DEFAULT_GUARD_VALIDATION_ARTIFACT_DIRECTORY,
            DEFAULT_GUARDED_ARTIFACT_DIRECTORY,
            DEFAULT_PRIMARY_CONTEXT_FIX_ARTIFACT_DIRECTORY,
        )
        for legacy_root in legacy_artifact_roots:
            legacy_resolved = _safe_path(legacy_root, "input_artifact_invalid")
            if output_root == legacy_resolved or legacy_resolved in output_root.parents:
                raise CompactStageADevelopmentError("authorization_binding_mismatch")
    if output_root.exists():
        raise FileExistsError("development_v3_output_is_immutable")
    stratified_mode = False
    lineage: Dict[str, Any] = {}
    selection_report_override: Optional[Mapping[str, Any]] = None
    guard_audit_hashes: Dict[str, str] = {}
    guard_audit_summary_value: Optional[Mapping[str, Any]] = None
    guard_human_rows: List[Mapping[str, Any]] = []
    guard_replay_value: Optional[Mapping[str, Any]] = None
    authority_projection_summary_value: Optional[Mapping[str, Any]] = None
    authority_projection_rows: Tuple[Mapping[str, Any], ...] = ()
    authority_projection_hashes_value: Dict[str, str] = {}
    authority_bound_canonical_mapping_rows: Tuple[Mapping[str, Any], ...] = ()
    topic_guided_review_summary_value: Optional[Mapping[str, Any]] = None
    topic_guided_review_hashes_value: Dict[str, str] = {}
    topic_guided_request_stats_value: Dict[str, Any] = {}
    topic_guided_preflight_requests: Tuple[Mapping[str, Any], ...] = ()
    if isinstance(input_directory, (str, Path)):
        input_path = _safe_path(input_directory, "input_artifact_invalid")
        if _is_stratified_current_path(input_path):
            context_path = context_directory if context_directory is not None else context_input_directory
            input_rows, input_store, input_manifest, input_hashes, lineage, selection_plan = _read_stratified_current_input(
                input_path,
                context_path,
                audit_directory,
                audit_summary=audit_summary,
                human_audit=human_audit,
            )
            input_label = str(input_path)
            stratified_mode = True
        else:
            input_rows, input_store, input_manifest, input_hashes = _read_v2_input(input_path)
            input_label = str(input_path)
    else:
        raw_pages = list(pages if pages is not None else input_directory)
        input_rows, input_store = _normalize_mapping_pages(raw_pages, store)
        input_manifest = dict(manifest or {"artifact_version": INPUT_ARTIFACT_VERSION, "split": "development", "local_day": LOCAL_DAY, "status": "synthetic"})
        input_hashes = {"manifest_sha256": stable_hash(input_manifest), "pages_sha256": stable_hash([item["page"] for item in input_rows]), "materialized_sha256": "", "store_sha256": stable_hash(input_store)}
        input_label = "<synthetic-linear-stage-packet-v2>"
    if not stratified_mode:
        input_binding = _input_binding_hash(input_manifest, input_rows, input_hashes, input_store)
        selection_plan = _selection_plan(input_rows, input_store)
    selected = list(selection_plan.selected)
    if topic_guided_authorization or authority_bound_authorization:
        # This authorization is tied to the latest reviewed five-page
        # projection.  Never let a normal selector fill, trim, or reorder the
        # audited set; a count/rank drift is a hard failure before any model
        # is constructed.
        if len(selected) != AUTHORITY_BOUND_MAX_SELECTED_PAGES:
            raise CompactStageADevelopmentError("authority_projection_page_drift")
        selected_ranks = [
            int(page.get("_selection_rank", index + 1))
            for index, (page, _categories) in enumerate(selected)
        ]
        if sorted(selected_ranks) != list(range(1, AUTHORITY_BOUND_MAX_SELECTED_PAGES + 1)):
            raise CompactStageADevelopmentError("authority_projection_page_drift")
        # Validate the actual selected source identities before consulting any
        # projection sidecar.  This closes the former gap where any five-page
        # selector output could be paired with the reviewed authority rows.
        _authority_bound_source_mapping(selected)
        source_selection_sha256 = str(lineage.get("selection_sha256", ""))
        if not source_selection_sha256:
            source_selection_sha256 = stable_hash(
                selection_plan.to_dict(selected_page_limit=AUTHORITY_BOUND_MAX_SELECTED_PAGES)
            )
        source_selection_sha256 = _authority_projection_sha256(
            source_selection_sha256,
            "authority_projection_hash_missing",
        )
        effective_authority_audit_root = (
            authority_projection_audit_directory
            if authority_projection_audit_directory is not None
            else authority_bound_audit_directory
        )
        effective_authority_audit_summary = (
            authority_projection_audit_summary
            if authority_projection_audit_summary is not None
            else authority_bound_audit_summary
        )
        if effective_authority_audit_summary is None:
            effective_authority_audit_summary = authority_projection_summary
        if effective_authority_audit_summary is None:
            effective_authority_audit_summary = authority_projection_audit
        if effective_authority_audit_summary is None:
            effective_authority_audit_summary = authority_bound_audit
        effective_authority_hashes = (
            authority_projection_hashes
            if authority_projection_hashes is not None
            else authority_bound_hashes
        )
        if effective_authority_audit_root is None and stratified_mode:
            effective_authority_audit_root = DEFAULT_AUTHORITY_BOUND_AUDIT_DIRECTORY
        (
            authority_projection_summary_value,
            authority_projection_rows,
            authority_projection_hashes_value,
        ) = _read_authority_projection_evidence(
            effective_authority_audit_root,
            selected=selected,
            source_selection_sha256=source_selection_sha256,
            summary_override=effective_authority_audit_summary,
            hash_override=effective_authority_hashes,
        )
        authority_bound_canonical_mapping_rows = _authority_bound_validate_canonical_mapping(
            selected,
            authority_projection_rows,
        )
    if guard_validation:
        # Guard mode is intentionally a separate selection surface.  It may
        # consume only the exact opaque refs that were reviewed in the
        # adapter-fix audit/replay; the ordinary selector is never allowed to
        # fill or replace a page here.
        effective_guard_audit_root = guard_audit_directory
        if (
            effective_guard_audit_root is None
            and guard_audit_summary is None
            and guard_human_audit is None
            and guards_replay is None
            and stratified_mode
        ):
            effective_guard_audit_root = DEFAULT_GUARD_VALIDATION_AUDIT_DIRECTORY
        (
            guard_audit_summary_value,
            guard_human_rows,
            guard_replay_value,
            guard_audit_hashes,
        ) = _read_guard_validation_evidence(
            effective_guard_audit_root,
            summary_override=guard_audit_summary,
            human_override=guard_human_audit,
            replay_override=guards_replay,
        )
        source_entries: Sequence[Mapping[str, Any]] = ()
        if stratified_mode:
            source_plan = input_manifest.get("candidate_selection_plan")
            global_plan = source_plan.get("global_candidate_plan") if isinstance(source_plan, Mapping) else None
            source_entries = global_plan.get("selected", ()) if isinstance(global_plan, Mapping) else ()
        _validate_guard_audited_subset(
            guard_subset_rows,
            available_pages=input_rows,
            source_entries=source_entries,
            human_rows=guard_human_rows,
            guards_replay=guard_replay_value,
        )
        page_by_ref = {
            _guard_subset_page_ref(
                item.get("page") if isinstance(item.get("page"), Mapping) else item,
                index,
            ): item.get("page") if isinstance(item.get("page"), Mapping) else item
            for index, item in enumerate(input_rows)
            if isinstance(item.get("page") if isinstance(item, Mapping) else None, Mapping)
            or isinstance(item, Mapping)
        }
        guard_selected: List[Tuple[Mapping[str, Any], Tuple[str, ...]]] = []
        for item in guard_subset_rows:
            target_ref = _guard_ref(item)
            page = page_by_ref.get(target_ref)
            if page is None:
                # A compact synthetic row may omit internal rank metadata; a
                # strict page/root/source/scope match is still required.
                candidates = [
                    candidate
                    for candidate_ref, candidate in page_by_ref.items()
                    if candidate_ref[1:] == target_ref[1:]
                ]
                if len(candidates) == 1:
                    page = candidates[0]
            if page is None:
                raise CompactStageADevelopmentError("guard_subset_invalid")
            mutable_page = dict(page)
            mutable_page["_guard_id"] = str(item["guard_id"])
            if item.get("expected_error_code"):
                mutable_page["_guard_expected_error_code"] = str(item["expected_error_code"])
            mutable_page["_guard_expected_category"] = str(item.get("expected_category", ""))
            mutable_page["_selection_rank"] = int(item["selection_rank"])
            categories = tuple(
                category
                for category in (
                    _canonical_stratum(value)
                    for value in (mutable_page.get("categories") or (item.get("cue_family"),))
                )
                if category
            )
            if not categories:
                categories = (str(item["cue_family"]),)
            guard_selected.append((mutable_page, categories))
        selected = guard_selected
        selection_plan = _stratified_plan(selected)
        missing_strata = ()
        selection_report_override = _guard_selection_report(
            selected,
            guard_subset_rows,
            source_selection_sha256=str(lineage.get("selection_sha256", "")),
        )
    if stratified_mode and not guard_validation and not guarded_authorization and not authority_bound_authorization:
        # Keep this historical formula untouched for AUTHORIZATION_ID.  The
        # repaired authorization below gets a distinct, richer binding.
        input_binding = _stratified_input_binding_hash(input_manifest, input_hashes, lineage, selected)
    if topic_guided_authorization or authority_bound_authorization:
        # The audited projection is a fixed five-page set; missing-category
        # diagnostics must not trigger an implicit supplement or selector
        # fallback.  The sidecar's exact-five gate is the only coverage claim.
        missing_strata = ()
    elif not guard_validation:
        missing_strata = () if stratified_mode else selection_plan.missing_strata
    if stratified_mode and not guard_validation and len(selected) != MAX_SELECTED_PAGES:
        raise CompactStageADevelopmentError("input_artifact_invalid")
    if guarded_authorization and len(selected) != GUARDED_MAX_SELECTED_PAGES:
        # The guarded authorization is tied to the reviewed five-page current
        # selection.  It may not silently run a smaller subset or supplement
        # the selection with pages chosen by a fallback selector.
        raise CompactStageADevelopmentError("input_artifact_invalid")
    if topic_guided_authorization and len(selected) != TOPIC_GUIDED_MAX_SELECTED_PAGES:
        raise CompactStageADevelopmentError("topic_guided_page_drift")
    if authority_bound_authorization and len(selected) != AUTHORITY_BOUND_MAX_SELECTED_PAGES:
        raise CompactStageADevelopmentError("authority_projection_page_drift")
    if not guard_validation and not guarded_authorization and not stratified_mode and len(selected) > MAX_SELECTED_PAGES:
        selected = selected[:MAX_SELECTED_PAGES]
    if not selected:
        raise CompactStageADevelopmentError("input_artifact_missing")
    scope = _scope(selected[0][0].get("scope"))
    if topic_guided_authorization and {
        _scope_pair(page.get("scope")) for page, _ in selected
    } != {_scope_pair(scope)}:
        raise CompactStageADevelopmentError("topic_guided_scope_drift")
    if authority_bound_authorization and {
        _scope_pair(page.get("scope")) for page, _ in selected
    } != {_scope_pair(scope)}:
        raise CompactStageADevelopmentError("authority_projection_scope_drift")
    if guard_validation and {
        _scope_pair(page.get("scope")) for page, _ in selected
    } != {_scope_pair(scope)}:
        raise CompactStageADevelopmentError("cross_chat_scope_violation")
    settings_file = _safe_path(settings_path, "input_artifact_invalid")
    actual_settings_sha256 = _file_sha256(settings_file)
    if topic_guided_authorization:
        # The topic-guided contract recomputes settings from disk.  A caller
        # supplied digest can only assert the recomputed value and can never
        # establish authority itself.
        expected_settings_sha256 = TOPIC_GUIDED_SETTINGS_SHA256
        if not actual_settings_sha256:
            raise CompactStageADevelopmentError("topic_guided_review_hash_missing")
        actual_settings_sha256 = _authority_projection_sha256(
            actual_settings_sha256,
            "topic_guided_review_hash_missing",
        )
        if actual_settings_sha256 != expected_settings_sha256:
            raise CompactStageADevelopmentError("topic_guided_review_hash_mismatch")
        if settings_sha256 is not None and str(settings_sha256).strip().lower() != actual_settings_sha256:
            raise CompactStageADevelopmentError("topic_guided_review_hash_mismatch")
        settings_sha256 = actual_settings_sha256
    elif authority_bound_authorization:
        # Settings are recomputed from the file, never accepted from the
        # caller as an authority source.  A supplied value is only checked as
        # an assertion against the actual/frozen digest.
        if actual_settings_sha256:
            actual_settings_sha256 = _authority_projection_sha256(
                actual_settings_sha256,
                "authority_projection_hash_missing",
            )
            if actual_settings_sha256 != AUTHORITY_BOUND_SETTINGS_SHA256:
                raise CompactStageADevelopmentError("authority_projection_hash_mismatch")
            if settings_sha256 is not None and str(settings_sha256).strip().lower() != actual_settings_sha256:
                raise CompactStageADevelopmentError("authority_projection_hash_mismatch")
            settings_sha256 = actual_settings_sha256
        else:
            if settings_sha256 is None:
                raise CompactStageADevelopmentError("authority_projection_hash_missing")
            settings_sha256 = _authority_projection_sha256(
                settings_sha256,
                "authority_projection_hash_missing",
            )
            if settings_sha256 != AUTHORITY_BOUND_SETTINGS_SHA256:
                raise CompactStageADevelopmentError("authority_projection_hash_mismatch")
    elif settings_sha256 is None:
        settings_sha256 = actual_settings_sha256
    if not settings_sha256:
        settings_sha256 = stable_hash({"settings_path": str(settings_file)})
    selected_model = model if model is not None else provider
    model_id = str(getattr(selected_model, "model_id", MODEL_ID)) if selected_model is not None else MODEL_ID
    source = str(getattr(selected_model, "source", SOURCE_ID)) if selected_model is not None else SOURCE_ID
    model_error: Optional[str] = None
    if (stratified_mode or authority_bound_authorization or topic_guided_authorization) and selected_model is not None and model_id != MODEL_ID:
        model_error = "model_mismatch"
        if authority_bound_authorization or topic_guided_authorization:
            # The new contract names the exact model.  Do not emit a partial
            # artifact whose ledger is bound to a different provider model.
            raise CompactStageADevelopmentError("model_mismatch")
        selected_model = None
    if stratified_mode:
        lineage = dict(lineage)
        lineage.update(
            {
                "settings_sha256": str(settings_sha256),
                "model": model_id,
                "protocol": PROTOCOL_VERSION,
                "scope_sha256": stable_hash(scope),
            }
        )
    adapter_metadata = _adapter_binding_metadata(selected_model)
    if guard_validation:
        # Synthetic inputs intentionally do not pretend to have a private
        # source/context artifact.  Their opaque hashes are still bound into
        # the authorization so a rerun with changed fixtures cannot reuse it.
        lineage = dict(lineage)
        if not lineage.get("selection_sha256"):
            lineage["selection_sha256"] = stable_hash(
                [
                    {
                        "selection_rank": int(item["selection_rank"]),
                        "page_ref": str(item["page_ref"]),
                        "root_ref": str(item["root_ref"]),
                        "source_ref": str(item["source_ref"]),
                        "scope_ref": str(item["scope_ref"]),
                        "cue_family": str(item["cue_family"]),
                    }
                    for item in guard_subset_rows
                ]
            )
        if not lineage.get("context_input_sha256"):
            lineage["context_input_sha256"] = str(
                input_hashes.get("context_input_sha256")
                or input_hashes.get("store_sha256")
                or stable_hash({"synthetic_context": input_store})
            )
        lineage.update(
            {
                "authorization_id": authorization_id,
                "adapter": adapter_metadata["adapter"],
                "adapter_source": adapter_metadata["adapter_source"],
                "adapter_sha256": adapter_metadata["adapter_sha256"],
                "adapter_code_sha256": adapter_metadata["adapter_code_sha256"],
                "code_sha256": adapter_metadata["code_sha256"],
                "source_code_sha256": _code_hash_fingerprint(input_manifest.get("code_sha256", "")),
                "protocol": PROTOCOL_VERSION,
                "settings_sha256": str(settings_sha256),
                "model": model_id,
                "scope_sha256": stable_hash(scope),
                "max_provider_calls": GUARD_VALIDATION_MAX_PROVIDER_CALLS,
                "per_page_provider_call_limit": GUARD_VALIDATION_PER_PAGE_PROVIDER_CALL_LIMIT,
                "max_retries": GUARD_VALIDATION_MAX_RETRIES,
                "sdk_calls": GUARD_VALIDATION_SDK_CALLS,
                "audit_authorization_source": (
                    "compact_stage_a_development_pilot_v3_stratified_current_adapter_fix/audit"
                    if stratified_mode
                    else "synthetic_test_only"
                ),
                "subset_sha256": _guard_subset_hash(guard_subset_rows),
                "guard_page_refs": [str(item["page_ref"]) for item in guard_subset_rows],
                "guard_ids": [str(item["guard_id"]) for item in guard_subset_rows],
                "guard_audit_summary_sha256": str(guard_audit_hashes.get("guard_audit_summary_sha256", "")),
                "guard_human_audit_sha256": str(guard_audit_hashes.get("guard_human_audit_sha256", "")),
                "guards_replay_sha256": str(guard_audit_hashes.get("guards_replay_sha256", "")),
                "guard_prompt_version": GUARD_VALIDATION_PROMPT_VERSION,
                "guard_taxonomy_version": GUARD_VALIDATION_TAXONOMY_VERSION,
                "guard_code_sha256": guard_code_sha256,
            }
        )
        input_binding = _guard_validation_input_binding_hash(
            input_manifest,
            input_hashes,
            lineage,
            selected,
            authorization_id=authorization_id,
            subset_sha256=str(lineage["subset_sha256"]),
            subset=guard_subset_rows,
            guard_audit_hashes=guard_audit_hashes,
            adapter_metadata=adapter_metadata,
            model=model_id,
            protocol=PROTOCOL_VERSION,
            settings_sha256=str(settings_sha256),
            scope=scope,
            guard_code_sha256=guard_code_sha256,
        )
    elif topic_guided_authorization:
        # The new pilot binds the existing authority projection plus the
        # latest body-free topic-quality gate.  Build and validate all five
        # requests before opening/reserving its independent ledger.
        lineage = dict(lineage)
        if not lineage.get("selection_sha256"):
            lineage["selection_sha256"] = stable_hash(selection_plan.to_dict(selected_page_limit=TOPIC_GUIDED_MAX_SELECTED_PAGES))
        if not lineage.get("context_input_sha256"):
            lineage["context_input_sha256"] = str(input_hashes.get("context_input_sha256") or input_hashes.get("store_sha256") or stable_hash({"synthetic_context": input_store}))
        if not lineage.get("audit_summary_sha256"):
            lineage["audit_summary_sha256"] = stable_hash(audit_summary) if audit_summary is not None else stable_hash({"audit_source": "synthetic_test_only", "allow_stage_a": True, "selected_page_count": TOPIC_GUIDED_MAX_SELECTED_PAGES, "selection_sha256": lineage["selection_sha256"]})
        if not lineage.get("human_audit_sha256"):
            lineage["human_audit_sha256"] = stable_hash(list(human_audit)) if human_audit is not None else stable_hash({"audit_source": "synthetic_test_only", "selected_page_count": TOPIC_GUIDED_MAX_SELECTED_PAGES, "selection_sha256": lineage["selection_sha256"]})
        authority_hashes = {str(key): str(value) for key, value in authority_projection_hashes_value.items()}
        authority_hashes.setdefault("authority_projection_selection_sha256", str(lineage["selection_sha256"]))
        authority_hashes.setdefault("authority_projection_context_sha256", str(lineage["context_input_sha256"]))
        authority_hashes.setdefault("authority_projection_audit_summary_sha256", str(lineage["audit_summary_sha256"]))
        authority_hashes.setdefault("authority_projection_human_audit_sha256", str(lineage["human_audit_sha256"]))
        for key, value in list(authority_hashes.items()):
            authority_hashes[key] = _authority_projection_sha256(value, "topic_guided_review_hash_missing")
        supplied_authority_hashes = authority_projection_hashes if authority_projection_hashes is not None else authority_bound_hashes
        if supplied_authority_hashes is not None:
            if not isinstance(supplied_authority_hashes, Mapping):
                raise CompactStageADevelopmentError("topic_guided_review_hash_missing")
            for raw_key, raw_value in supplied_authority_hashes.items():
                key = str(raw_key)
                supplied = _authority_projection_sha256(raw_value, "topic_guided_review_hash_missing")
                if key not in authority_hashes or authority_hashes[key] != supplied:
                    raise CompactStageADevelopmentError("topic_guided_review_hash_mismatch")
        authority_page_refs = tuple(str(row.get("page_ref") or "") for row in authority_projection_rows)
        authority_scope_refs = tuple(str(row.get("scope_ref") or "") for row in authority_projection_rows)
        if len(authority_page_refs) != TOPIC_GUIDED_MAX_SELECTED_PAGES or any(not ref for ref in authority_page_refs):
            raise CompactStageADevelopmentError("topic_guided_page_drift")
        if len(set(authority_scope_refs)) != 1 or not authority_scope_refs[0]:
            raise CompactStageADevelopmentError("topic_guided_scope_drift")
        preflight: List[Mapping[str, Any]] = []
        for page, _categories in selected:
            try:
                preflight.append(_build_request(page, input_store))
            except CompactStageADevelopmentError as exc:
                if str(getattr(exc, "code", exc)) in {"request_primary_messages_empty", "provider_primary_role_invalid", "page_no_primary_message"}:
                    raise CompactStageADevelopmentError("topic_guided_page_drift") from exc
                raise
        topic_guided_preflight_requests = tuple(preflight)
        topic_guided_request_stats_value = _topic_guided_validate_requests(selected, topic_guided_preflight_requests, stratified_mode=stratified_mode)
        effective_topic_review_root = topic_guided_review_directory or topic_guided_audit_directory
        effective_topic_review_summary = topic_guided_review_summary if topic_guided_review_summary is not None else topic_guided_audit_summary
        if effective_topic_review_summary is None:
            effective_topic_review_summary = topic_guided_summary
        if effective_topic_review_root is None and stratified_mode:
            effective_topic_review_root = DEFAULT_TOPIC_GUIDED_REVIEW_DIRECTORY
        topic_guided_review_summary_value, topic_guided_review_hashes_value = _read_topic_guided_review_evidence(
            effective_topic_review_root,
            selected=selected,
            requests=topic_guided_preflight_requests,
            source_selection_sha256=str(lineage["selection_sha256"]),
            summary_override=effective_topic_review_summary,
        )
        supplied_topic_hashes = topic_guided_hashes if topic_guided_hashes is not None else topic_guided_review_hashes
        if supplied_topic_hashes is not None:
            if not isinstance(supplied_topic_hashes, Mapping):
                raise CompactStageADevelopmentError("topic_guided_review_hash_missing")
            for raw_key, raw_value in supplied_topic_hashes.items():
                key = str(raw_key)
                supplied = _authority_projection_sha256(raw_value, "topic_guided_review_hash_missing")
                if key not in topic_guided_review_hashes_value or topic_guided_review_hashes_value[key] != supplied:
                    raise CompactStageADevelopmentError("topic_guided_review_hash_mismatch")
        guidance_sha256 = _topic_guided_guidance_sha256()
        grouping_sha256 = _topic_guided_grouping_hints_sha256()
        if topic_guided_guidance_sha256 is not None and str(topic_guided_guidance_sha256).strip().lower() != guidance_sha256:
            raise CompactStageADevelopmentError("topic_guided_guidance_drift")
        if topic_guided_grouping_hints_sha256 is not None and str(topic_guided_grouping_hints_sha256).strip().lower() != grouping_sha256:
            raise CompactStageADevelopmentError("topic_guided_grouping_hint_drift")
        if topic_guided_g_hint_binding_sha256 is not None and str(topic_guided_g_hint_binding_sha256).strip().lower() != str(topic_guided_request_stats_value["g_hint_binding_sha256"]).lower():
            raise CompactStageADevelopmentError("topic_guided_grouping_hint_drift")
        topic_code_sha256 = _topic_guided_code_sha256()
        lineage.update(
            {
                "authorization_id": authorization_id,
                "adapter": adapter_metadata["adapter"],
                "adapter_source": adapter_metadata["adapter_source"],
                "adapter_sha256": adapter_metadata["adapter_sha256"],
                "adapter_code_sha256": adapter_metadata["adapter_code_sha256"],
                "code_sha256": adapter_metadata["code_sha256"],
                "source_code_sha256": _code_hash_fingerprint(input_manifest.get("code_sha256", "")),
                "topic_guided_code_sha256": topic_code_sha256,
                "topic_guided_prompt_version": TOPIC_GUIDED_PROMPT_VERSION,
                "topic_guided_prompt_sha256": stable_hash(TOPIC_GUIDED_SYSTEM_PROMPT),
                "topic_guided_guidance_version": TOPIC_GUIDED_GUIDANCE_VERSION,
                "topic_guided_guidance_sha256": guidance_sha256,
                "topic_guided_grouping_hints_version": TOPIC_GUIDED_GROUPING_HINTS_VERSION,
                "topic_guided_grouping_hints_policy_sha256": grouping_sha256,
                "topic_guided_g_hint_binding_sha256": str(topic_guided_request_stats_value["g_hint_binding_sha256"]),
                "topic_guided_protocol_source_sha256": _topic_guided_protocol_source_sha256(),
                "topic_guided_taxonomy_version": TOPIC_GUIDED_TAXONOMY_VERSION,
                "topic_guided_review_hashes": dict(topic_guided_review_hashes_value),
                "protocol": PROTOCOL_VERSION,
                "settings_sha256": str(settings_sha256),
                "model": model_id,
                "scope_sha256": stable_hash(scope),
                "max_total_calls": TOPIC_GUIDED_MAX_TOTAL_CALLS,
                "max_provider_calls": TOPIC_GUIDED_MAX_PROVIDER_CALLS,
                "per_page_provider_call_limit": TOPIC_GUIDED_PER_PAGE_PROVIDER_CALL_LIMIT,
                "max_retries": TOPIC_GUIDED_MAX_RETRIES,
                "supplement_calls": TOPIC_GUIDED_SUPPLEMENT_CALLS,
                "supplement_pages": TOPIC_GUIDED_SUPPLEMENT_PAGES,
                "health_calls": TOPIC_GUIDED_HEALTH_CALLS,
                "sdk_calls": TOPIC_GUIDED_SDK_CALLS,
                "no_supplement": True,
                "response_format_mode": RESPONSE_FORMAT_MODE,
                "response_format_sent": False,
                "thinking_disabled": THINKING_DISABLED,
                "stage_b_authorized": False,
                "stage_c_authorized": False,
                "production_authorized": False,
                "frozen_read": False,
                "gold_loaded": False,
                "topic_guided_authorization": True,
                "topic_guided": True,
                "authority_bound_projection": True,
                "authority_projection_hashes": authority_hashes,
                "authority_bound_canonical_mapping_version": TOPIC_GUIDED_CANONICAL_MAPPING_VERSION,
                "authority_bound_canonical_mapping_sha256": TOPIC_GUIDED_CANONICAL_MAPPING_SHA256,
                "authority_bound_canonical_mapping": [dict(row) for row in authority_bound_canonical_mapping_rows],
                "authority_projection_page_refs": list(authority_page_refs),
                "authority_projection_scope_ref": authority_scope_refs[0],
                "authority_projection_binding_sha256": str(authority_hashes.get("authority_projection_binding_sha256", "")),
                "topic_guided_review_sha256": str(topic_guided_review_hashes_value.get("topic_guided_review_sha256", "")),
                "audit_authorization_source": "compact_stage_a_development_pilot_v3_stratified_current_guarded/audit" if stratified_mode else "synthetic_test_only",
            }
        )
        if not lineage["authority_projection_binding_sha256"]:
            raise CompactStageADevelopmentError("topic_guided_review_hash_missing")
        input_binding = _topic_guided_input_binding_hash(
            input_manifest, input_hashes, lineage, selected,
            authorization_id=authorization_id, adapter_metadata=adapter_metadata,
            model=model_id, protocol=PROTOCOL_VERSION, settings_sha256=str(settings_sha256),
            scope=scope, authority_projection_hashes=authority_hashes,
            authority_projection_page_refs=authority_page_refs,
            topic_guided_review_hashes=topic_guided_review_hashes_value,
            request_stats=topic_guided_request_stats_value,
        )
        _topic_guided_validate_frozen_boundary(
            stratified_mode=stratified_mode, lineage=lineage,
            authority_projection_hashes=authority_hashes,
            topic_guided_review_hashes=topic_guided_review_hashes_value,
            settings_sha256=str(settings_sha256), scope=scope,
            topic_guided_code_sha256=topic_code_sha256,
            request_stats=topic_guided_request_stats_value, input_hashes=input_hashes,
        )
    elif authority_bound_authorization:
        # The authority-bound contract is deliberately self-contained.  It
        # binds the current source selection/context/audit hashes, the latest
        # body-free projection review, and the execution policy before a
        # ledger is opened.  No audit body or provider payload is copied.
        lineage = dict(lineage)
        if not lineage.get("selection_sha256"):
            lineage["selection_sha256"] = stable_hash(
                selection_plan.to_dict(selected_page_limit=AUTHORITY_BOUND_MAX_SELECTED_PAGES)
            )
        if not lineage.get("context_input_sha256"):
            lineage["context_input_sha256"] = str(
                input_hashes.get("context_input_sha256")
                or input_hashes.get("store_sha256")
                or stable_hash({"synthetic_context": input_store})
            )
        if not lineage.get("audit_summary_sha256"):
            if audit_summary is not None:
                _body_free(audit_summary)
                lineage["audit_summary_sha256"] = stable_hash(audit_summary)
            else:
                lineage["audit_summary_sha256"] = stable_hash(
                    {
                        "audit_source": "synthetic_test_only",
                        "allow_stage_a": True,
                        "selected_page_count": AUTHORITY_BOUND_MAX_SELECTED_PAGES,
                        "selection_sha256": lineage["selection_sha256"],
                    }
                )
        if not lineage.get("human_audit_sha256"):
            if human_audit is not None:
                human_rows = list(human_audit)
                _body_free(human_rows)
                lineage["human_audit_sha256"] = stable_hash(human_rows)
            else:
                lineage["human_audit_sha256"] = stable_hash(
                    {
                        "audit_source": "synthetic_test_only",
                        "selected_page_count": AUTHORITY_BOUND_MAX_SELECTED_PAGES,
                        "selection_sha256": lineage["selection_sha256"],
                    }
                )
        authority_hashes = {
            str(key): str(value)
            for key, value in authority_projection_hashes_value.items()
        }
        authority_hashes.setdefault(
            "authority_projection_selection_sha256",
            str(lineage["selection_sha256"]),
        )
        authority_hashes.setdefault(
            "authority_projection_context_sha256",
            str(lineage["context_input_sha256"]),
        )
        authority_hashes.setdefault(
            "authority_projection_audit_summary_sha256",
            str(lineage["audit_summary_sha256"]),
        )
        authority_hashes.setdefault(
            "authority_projection_human_audit_sha256",
            str(lineage["human_audit_sha256"]),
        )
        for key, value in authority_hashes.items():
            authority_hashes[key] = _authority_projection_sha256(value)
        # A caller may provide a hash assertion for compatibility, but it can
        # never populate or replace the recomputed map.  Compare only after
        # sidecar lineage and runner fallbacks have been resolved.
        if effective_authority_hashes is not None:
            if not isinstance(effective_authority_hashes, Mapping):
                raise CompactStageADevelopmentError("authority_projection_hash_missing")
            for raw_key, raw_value in effective_authority_hashes.items():
                key = str(raw_key)
                supplied = _authority_projection_sha256(raw_value)
                if key not in authority_hashes or authority_hashes[key] != supplied:
                    raise CompactStageADevelopmentError("authority_projection_hash_mismatch")
        authority_page_refs = tuple(
            str(row["page_ref"]) for row in authority_projection_rows
        )
        authority_scope_refs = tuple(
            str(row.get("scope_ref") or "") for row in authority_projection_rows
        )
        if len(authority_page_refs) != AUTHORITY_BOUND_MAX_SELECTED_PAGES or any(
            not ref for ref in authority_page_refs
        ):
            raise CompactStageADevelopmentError("authority_projection_page_drift")
        if len(set(authority_scope_refs)) != 1 or not authority_scope_refs[0]:
            raise CompactStageADevelopmentError("authority_projection_scope_drift")
        lineage.update(
            {
                "authorization_id": authorization_id,
                "adapter": adapter_metadata["adapter"],
                "adapter_source": adapter_metadata["adapter_source"],
                "adapter_sha256": adapter_metadata["adapter_sha256"],
                "adapter_code_sha256": adapter_metadata["adapter_code_sha256"],
                "code_sha256": adapter_metadata["code_sha256"],
                "source_code_sha256": _code_hash_fingerprint(input_manifest.get("code_sha256", "")),
                "authority_bound_code_sha256": guard_code_sha256,
                "authority_bound_prompt_version": AUTHORITY_BOUND_PROMPT_VERSION,
                "authority_bound_prompt_sha256": stable_hash(AUTHORITY_BOUND_SYSTEM_PROMPT),
                "authority_bound_taxonomy_version": AUTHORITY_BOUND_TAXONOMY_VERSION,
                "protocol": PROTOCOL_VERSION,
                "settings_sha256": str(settings_sha256),
                "model": model_id,
                "scope_sha256": stable_hash(scope),
                "max_total_calls": AUTHORITY_BOUND_MAX_TOTAL_CALLS,
                "max_provider_calls": AUTHORITY_BOUND_MAX_PROVIDER_CALLS,
                "per_page_provider_call_limit": AUTHORITY_BOUND_PER_PAGE_PROVIDER_CALL_LIMIT,
                "max_retries": AUTHORITY_BOUND_MAX_RETRIES,
                "supplement_calls": AUTHORITY_BOUND_SUPPLEMENT_CALLS,
                "supplement_pages": AUTHORITY_BOUND_SUPPLEMENT_PAGES,
                "health_calls": AUTHORITY_BOUND_HEALTH_CALLS,
                "sdk_calls": AUTHORITY_BOUND_SDK_CALLS,
                "no_supplement": True,
                "response_format_mode": RESPONSE_FORMAT_MODE,
                "response_format_sent": False,
                "thinking_disabled": THINKING_DISABLED,
                "stage_b_authorized": False,
                "stage_c_authorized": False,
                "production_authorized": False,
                "frozen_read": False,
                "gold_loaded": False,
                "authority_bound_authorization": True,
                "authority_bound": True,
                "audit_authorization_source": (
                    "compact_stage_a_development_pilot_v3_stratified_current_guarded/audit"
                    if stratified_mode
                    else "synthetic_test_only"
                ),
                "authority_projection_hashes": authority_hashes,
                "authority_bound_canonical_mapping_version": AUTHORITY_BOUND_CANONICAL_MAPPING_VERSION,
                "authority_bound_canonical_mapping_sha256": AUTHORITY_BOUND_CANONICAL_MAPPING_SHA256,
                "authority_bound_canonical_mapping": [
                    dict(row) for row in authority_bound_canonical_mapping_rows
                ],
                "authority_projection_page_refs": list(authority_page_refs),
                "authority_projection_scope_ref": authority_scope_refs[0],
                "authority_projection_binding_sha256": str(
                    authority_projection_hashes_value.get(
                        "authority_projection_binding_sha256", ""
                    )
                ),
            }
        )
        if not lineage["authority_projection_binding_sha256"]:
            raise CompactStageADevelopmentError("authority_projection_hash_missing")
        input_binding = _authority_bound_input_binding_hash(
            input_manifest,
            input_hashes,
            lineage,
            selected,
            authorization_id=authorization_id,
            adapter_metadata=adapter_metadata,
            model=model_id,
            protocol=PROTOCOL_VERSION,
            settings_sha256=str(settings_sha256),
            scope=scope,
            authority_projection_hashes=authority_hashes,
            authority_projection_page_refs=authority_page_refs,
        )
        _authority_bound_validate_frozen_boundary(
            stratified_mode=stratified_mode,
            lineage=lineage,
            authority_projection_hashes=authority_hashes,
            settings_sha256=str(settings_sha256),
            scope=scope,
            authority_bound_code_sha256=guard_code_sha256,
        )
    elif guarded_authorization:
        # The guarded authorization uses the same current five-page
        # selection/audit/context lineage as the adapter-fix path, but binds a
        # separate guard-code fingerprint and a separate immutable budget.
        # Synthetic tests derive deterministic body-free placeholders; a
        # stratified current input already supplies the audited hashes above.
        lineage = dict(lineage)
        if not lineage.get("selection_sha256"):
            lineage["selection_sha256"] = stable_hash(
                selection_plan.to_dict(selected_page_limit=GUARDED_MAX_SELECTED_PAGES)
            )
        if not lineage.get("context_input_sha256"):
            lineage["context_input_sha256"] = str(
                input_hashes.get("context_input_sha256")
                or input_hashes.get("store_sha256")
                or stable_hash({"synthetic_context": input_store})
            )
        if not lineage.get("audit_summary_sha256"):
            lineage["audit_summary_sha256"] = (
                stable_hash(audit_summary)
                if audit_summary is not None
                else stable_hash(
                    {
                        "audit_source": "synthetic_test_only",
                        "allow_stage_a": True,
                        "selected_page_count": GUARDED_MAX_SELECTED_PAGES,
                        "selection_sha256": lineage["selection_sha256"],
                    }
                )
            )
        if not lineage.get("human_audit_sha256"):
            lineage["human_audit_sha256"] = (
                stable_hash(list(human_audit))
                if human_audit is not None
                else stable_hash(
                    {
                        "audit_source": "synthetic_test_only",
                        "selected_page_count": GUARDED_MAX_SELECTED_PAGES,
                        "selection_sha256": lineage["selection_sha256"],
                    }
                )
            )
        lineage.update(
            {
                "authorization_id": authorization_id,
                "adapter": adapter_metadata["adapter"],
                "adapter_source": adapter_metadata["adapter_source"],
                "adapter_sha256": adapter_metadata["adapter_sha256"],
                "adapter_code_sha256": adapter_metadata["adapter_code_sha256"],
                "code_sha256": adapter_metadata["code_sha256"],
                "source_code_sha256": _code_hash_fingerprint(input_manifest.get("code_sha256", "")),
                "protocol": PROTOCOL_VERSION,
                "settings_sha256": str(settings_sha256),
                "model": model_id,
                "scope_sha256": stable_hash(scope),
                "max_provider_calls": GUARDED_MAX_PROVIDER_CALLS,
                "per_page_provider_call_limit": GUARDED_PER_PAGE_PROVIDER_CALL_LIMIT,
                "max_retries": GUARDED_MAX_RETRIES,
                "sdk_calls": GUARDED_SDK_CALLS,
                "audit_authorization_source": (
                    "linear_stage_packet_development_stratified_signals_current/audit"
                    if stratified_mode
                    else "synthetic_test_only"
                ),
                "guarded_authorization": True,
                "guard_prompt_version": GUARDED_PROMPT_VERSION,
                "guard_taxonomy_version": GUARDED_TAXONOMY_VERSION,
                "guard_code_sha256": guard_code_sha256,
                "guard_page_refs": [
                    str(page.get("page_id", "")) for page, _categories in selected
                ],
                "no_supplement": True,
                "supplement_calls": 0,
                "supplement_pages": 0,
            }
        )
        input_binding = _guarded_input_binding_hash(
            input_manifest,
            input_hashes,
            lineage,
            selected,
            authorization_id=authorization_id,
            adapter_metadata=adapter_metadata,
            model=model_id,
            protocol=PROTOCOL_VERSION,
            settings_sha256=str(settings_sha256),
            scope=scope,
            guard_code_sha256=guard_code_sha256,
        )
    elif adapter_fix_authorization:
        # Synthetic inputs remain useful for offline contract tests.  They do
        # not have an external audit sidecar, so derive explicit deterministic
        # placeholders from the selected plan/store and label them as such;
        # real stratified input always gets the verified current sidecar hashes
        # from _read_stratified_current_input above.
        lineage = dict(lineage)
        if not lineage.get("selection_sha256"):
            lineage["selection_sha256"] = stable_hash(selection_plan.to_dict())
        if not lineage.get("context_input_sha256"):
            lineage["context_input_sha256"] = str(
                input_hashes.get("context_input_sha256")
                or input_hashes.get("store_sha256")
                or stable_hash({"synthetic_context": input_store})
            )
        if not lineage.get("audit_summary_sha256"):
            lineage["audit_summary_sha256"] = (
                stable_hash(audit_summary)
                if audit_summary is not None
                else stable_hash(
                    {
                        "audit_source": "synthetic_test_only",
                        "allow_stage_a": True,
                        "selection_sha256": lineage["selection_sha256"],
                    }
                )
            )
        if not lineage.get("human_audit_sha256"):
            lineage["human_audit_sha256"] = (
                stable_hash(list(human_audit))
                if human_audit is not None
                else stable_hash(
                    {
                        "audit_source": "synthetic_test_only",
                        "selection_sha256": lineage["selection_sha256"],
                    }
                )
            )
        lineage.update(
            {
                "authorization_id": authorization_id,
                "adapter": adapter_metadata["adapter"],
                "adapter_source": adapter_metadata["adapter_source"],
                "adapter_sha256": adapter_metadata["adapter_sha256"],
                "adapter_code_sha256": adapter_metadata["adapter_code_sha256"],
                "code_sha256": adapter_metadata["code_sha256"],
                "source_code_sha256": _code_hash_fingerprint(input_manifest.get("code_sha256", "")),
                "protocol": PROTOCOL_VERSION,
                "settings_sha256": str(settings_sha256),
                "model": model_id,
                "scope_sha256": stable_hash(scope),
                "max_provider_calls": MAX_PROVIDER_CALLS,
                "per_page_provider_call_limit": PER_PAGE_PROVIDER_CALL_LIMIT,
                "max_retries": MAX_RETRIES,
                "audit_authorization_source": (
                    "linear_stage_packet_development_stratified_signals_current/audit"
                    if stratified_mode
                    else "synthetic_test_only"
                ),
            }
        )
        input_binding = _adapter_fix_input_binding_hash(
            input_manifest,
            input_hashes,
            lineage,
            selected,
            authorization_id=authorization_id,
            adapter_metadata=adapter_metadata,
            model=model_id,
            protocol=PROTOCOL_VERSION,
            settings_sha256=str(settings_sha256),
            scope=scope,
        )
    if topic_guided_authorization:
        # Health/reuse probes are outside the fresh topic-guided budget.
        if health_artifact_directory is not None or health_verified is True:
            raise CompactStageADevelopmentError("topic_guided_review_invalid")
        health_ok, health_meta = False, {
            "status": "disabled_by_topic_guided_contract",
            "strict_complete": False,
            "calls": 0,
        }
    elif authority_bound_authorization:
        # Authority-bound execution has no health probe/reuse budget.  A
        # caller may explicitly pass ``False`` as a compatibility marker, but
        # any health artifact/positive verification is outside this contract.
        if health_artifact_directory is not None or health_verified is True:
            raise CompactStageADevelopmentError("authority_projection_invalid")
        health_ok, health_meta = False, {
            "status": "disabled_by_authority_bound_contract",
            "strict_complete": False,
            "calls": 0,
        }
    else:
        health_ok, health_meta = _health_metadata(health_artifact_directory)
        if health_verified is not None:
            health_ok = bool(health_verified)
    # Synthetic injected models may be used offline without opening or reading
    # a health artifact.  Real adapters must pass an explicit verified health
    # artifact/flag; this guard prevents an accidental default network path.
    if selected_model is not None and health_artifact_directory is not None and not health_ok:
        selected_model = None

    authorization_contract = (
        _topic_guided_contract(
            authorization_id=authorization_id,
            adapter_metadata=adapter_metadata,
            model=model_id,
            protocol=PROTOCOL_VERSION,
            settings_sha256=str(settings_sha256),
            scope=scope,
            lineage=lineage,
            input_binding_sha256=input_binding,
            selected=selected,
            request_stats=topic_guided_request_stats_value,
            source_code_sha256=_code_hash_fingerprint(input_manifest.get("code_sha256", "")),
        )
        if topic_guided_authorization
        else _authority_bound_contract(
            authorization_id=authorization_id,
            adapter_metadata=adapter_metadata,
            model=model_id,
            protocol=PROTOCOL_VERSION,
            settings_sha256=str(settings_sha256),
            scope=scope,
            lineage=lineage,
            input_binding_sha256=input_binding,
            selected=selected,
            source_code_sha256=_code_hash_fingerprint(input_manifest.get("code_sha256", "")),
        )
        if authority_bound_authorization
        else _guard_validation_contract(
            authorization_id=authorization_id,
            adapter_metadata=adapter_metadata,
            model=model_id,
            protocol=PROTOCOL_VERSION,
            settings_sha256=str(settings_sha256),
            scope=scope,
            lineage=lineage,
            input_binding_sha256=input_binding,
            source_code_sha256=_code_hash_fingerprint(input_manifest.get("code_sha256", "")),
        )
        if guard_validation
        else _guarded_contract(
            authorization_id=authorization_id,
            adapter_metadata=adapter_metadata,
            model=model_id,
            protocol=PROTOCOL_VERSION,
            settings_sha256=str(settings_sha256),
            scope=scope,
            lineage=lineage,
            input_binding_sha256=input_binding,
            guard_code_sha256=guard_code_sha256,
            selected=selected,
            source_code_sha256=_code_hash_fingerprint(input_manifest.get("code_sha256", "")),
        )
        if guarded_authorization
        else _adapter_fix_contract(
            authorization_id=authorization_id,
            adapter_metadata=adapter_metadata,
            model=model_id,
            protocol=PROTOCOL_VERSION,
            settings_sha256=str(settings_sha256),
            scope=scope,
            lineage=lineage,
            input_binding_sha256=input_binding,
            source_code_sha256=_code_hash_fingerprint(input_manifest.get("code_sha256", "")),
        )
        if adapter_fix_authorization
        else None
    )
    try:
        ledger = CallAuthorizationLedger.for_authorization(
            _safe_path(authority_root, "input_artifact_invalid"),
            authorization_id=authorization_id,
            max_calls=provider_call_limit,
            provider=PROVIDER_ID,
            model=model_id,
            protocol=PROTOCOL_VERSION,
            settings_sha256=settings_sha256,
            scope=scope,
            input_sha256=input_binding,
            artifact_namespace=artifact_namespace,
        )
    except AuthorizationBindingMismatch as exc:
        # Keep the runner API body-free and deterministic when a caller tries
        # to reuse either authorization with changed settings/source/model.
        raise CompactStageADevelopmentError("authorization_binding_mismatch") from exc
    snapshot = ledger.snapshot()
    history_detected = bool(snapshot.get("reservation_count") or snapshot.get("rejection_count"))
    runs: List[PageRun] = []
    for index, (page, categories) in enumerate(selected):
        if topic_guided_authorization:
            # Reuse the preflight request that was validated before ledger
            # creation; never rebuild a caller-controlled payload after the
            # reserve boundary.
            request = topic_guided_preflight_requests[index]
            if history_detected:
                runs.append(PageRun(page, request, stable_hash(request), categories, "pending", None, None, "authorization_history_detected", False, False, 0, 0, 0.0, model_id, source, tuple(categories)))
                continue
            if selected_model is None:
                runs.append(PageRun(page, request, stable_hash(request), categories, "pending", None, None, model_error or "model_unconfigured", False, False, 0, 0, 0.0, model_id, source, tuple(categories)))
                continue
            runs.append(
                _run_one(
                    selected_model,
                    page,
                    request,
                    categories,
                    ledger,
                    input_sha256=input_binding,
                    settings_sha256=settings_sha256,
                    scope=scope,
                    cache=cache,
                    system_prompt=system_prompt,
                    guard_id=None,
                    artifact_namespace=artifact_namespace,
                )
            )
            continue
        if selection_plan.scope_authorization_required:
            blocked_request: Mapping[str, Any] = {"h": ()}
            runs.append(PageRun(page, blocked_request, stable_hash({"page_id": str(page.get("page_id", "")), "selection_blocked": True}), categories, "pending", None, None, "multi_scope_authorization_required", False, False, 0, 0, 0.0, model_id, source, tuple(categories)))
            continue
        try:
            request = _build_request(page, input_store)
        except CompactStageADevelopmentError as exc:
            # A page containing only greeting/ack/media context is retained
            # for recovery, but cannot form a valid Stage-A topic request.
            # Mark it pending without reserving budget or constructing a
            # provider call; a later substantive page may still use the same
            # bounded authorization.
            if str(getattr(exc, "code", exc)) not in {"request_primary_messages_empty", "provider_primary_role_invalid", "page_no_primary_message"}:
                raise
            blocked_request: Mapping[str, Any] = {"h": ()}
            runs.append(
                PageRun(
                    page,
                    blocked_request,
                    stable_hash({"page_id": str(page.get("page_id", "")), "context_only": True}),
                    categories,
                    "pending",
                    None,
                    None,
                    "context_only_page",
                    False,
                    False,
                    0,
                    0,
                    0.0,
                    model_id,
                    source,
                    tuple(categories),
                )
            )
            continue
        if stratified_mode or guarded_authorization or authority_bound_authorization:
            try:
                _validate_provider_primary_role(request)
            except CompactStageADevelopmentError as exc:
                blocked_request = request
                runs.append(
                    PageRun(
                        page,
                        blocked_request,
                        stable_hash(blocked_request),
                        categories,
                        "pending",
                        None,
                        None,
                        str(getattr(exc, "code", "provider_primary_role_invalid")),
                        False,
                        False,
                        0,
                        0,
                        0.0,
                        model_id,
                        source,
                        tuple(categories),
                    )
                )
                continue
        if history_detected:
            runs.append(PageRun(page, request, stable_hash(request), categories, "pending", None, None, "authorization_history_detected", False, False, 0, 0, 0.0, model_id, source, tuple(categories)))
            continue
        if selected_model is None:
            runs.append(PageRun(page, request, stable_hash(request), categories, "pending", None, None, model_error or "model_unconfigured", False, False, 0, 0, 0.0, model_id, source, tuple(categories)))
            continue
        runs.append(
            _run_one(
                selected_model,
                page,
                request,
                categories,
                ledger,
                input_sha256=input_binding,
                settings_sha256=settings_sha256,
                scope=scope,
                cache=cache,
                system_prompt=system_prompt,
                guard_id=str(page.get("_guard_id") or "") if guard_validation else None,
                artifact_namespace=artifact_namespace,
            )
        )
    health_reused = bool(health_ok)
    paths, aggregate, status, success = _write_artifacts(
        output_root,
        authorization_id=authorization_id,
        authorization_contract=authorization_contract,
        input_label=input_label,
        input_binding_sha256=input_binding,
        input_hashes=input_hashes,
        manifest_input=input_manifest,
        selected=selected,
        runs=runs,
        missing_strata=missing_strata,
        selection_plan=selection_plan,
        ledger=ledger,
        health_reused=health_reused,
        provider_model=model_id,
        provider_source=source,
        lineage=lineage,
        input_artifact_version=str(input_manifest.get("artifact_version") or INPUT_ARTIFACT_VERSION),
        selection_report_override=selection_report_override,
        artifact_version=artifact_version,
        selected_page_limit=selected_page_limit,
        provider_call_limit=provider_call_limit,
        per_page_provider_call_limit=per_page_provider_call_limit,
        max_retries=max_retries,
        sdk_calls=sdk_calls,
        guard_validation=guard_validation,
        guarded_authorization=guarded_authorization,
        authority_bound_authorization=authority_bound_authorization,
        topic_guided_authorization=topic_guided_authorization,
        guard_prompt_version=(
            GUARD_VALIDATION_PROMPT_VERSION
            if guard_validation
            else GUARDED_PROMPT_VERSION
            if guarded_authorization
            else TOPIC_GUIDED_PROMPT_VERSION
            if topic_guided_authorization
            else AUTHORITY_BOUND_PROMPT_VERSION
            if authority_bound_authorization
            else None
        ),
        guard_taxonomy_version=(
            GUARD_VALIDATION_TAXONOMY_VERSION
            if guard_validation
            else GUARDED_TAXONOMY_VERSION
            if guarded_authorization
            else TOPIC_GUIDED_TAXONOMY_VERSION
            if topic_guided_authorization
            else AUTHORITY_BOUND_TAXONOMY_VERSION
            if authority_bound_authorization
            else None
        ),
        guard_code_sha256=guard_code_sha256 if (guard_validation or guarded_authorization or authority_bound_authorization or topic_guided_authorization) else None,
        artifact_namespace=artifact_namespace,
    )
    return CompactStageADevelopmentResult(
        input_directory=input_label,
        output_directory=str(output_root),
        status=status,
        success=success,
        selected_page_count=len(selected),
        provider_calls=sum(bool(run.provider_call) for run in runs),
        pending_count=sum(run.status != "complete" for run in runs),
        missing_strata=tuple(missing_strata),
        aggregate=aggregate,
        artifact_paths=paths,
        selection_diagnostics=(selection_report_override or selection_plan.to_dict(selected_page_limit=selected_page_limit)),
    )


run_stage_a_development_pilot_v3 = run_compact_stage_a_development_pilot_v3
run_compact_stage_a_development_v3 = run_compact_stage_a_development_pilot_v3


__all__ = [
    "ARTIFACT_VERSION",
    "ARTIFACT_NAMESPACE",
    "AUTHORIZATION_ID",
    "ADAPTER_FIX_AUTHORIZATION_ID",
    "AUTHORIZATION_ID_ADAPTER_FIX",
    "STRATIFIED_CURRENT_STAGE_A_V3_PILOT_ADAPTER_FIX_20260829",
    "GUARD_VALIDATION_AUTHORIZATION_ID",
    "AUTHORIZATION_ID_GUARD_VALIDATION_3",
    "STRATIFIED_CURRENT_STAGE_A_V3_GUARD_VALIDATION_3_20260829",
    "GUARDED_AUTHORIZATION_ID",
    "AUTHORIZATION_ID_GUARDED",
    "STRATIFIED_CURRENT_STAGE_A_V3_PILOT_GUARDED_20260830",
    "AUTHORITY_BOUND_AUTHORIZATION_ID",
    "AUTHORIZATION_ID_AUTHORITY_BOUND",
    "STRATIFIED_CURRENT_STAGE_A_V3_PILOT_AUTHORITY_BOUND_20260830",
    "ALLOWED_AUTHORIZATION_IDS",
    "CATEGORY_NAMES",
    "CompactStageAModel",
    "CompactStageAResponse",
    "CompactStageADevelopmentError",
    "CompactStageADevelopmentResult",
    "CompactStageASelectionPlan",
    "DEFAULT_ARTIFACT_DIRECTORY",
    "DEFAULT_AUTHORITY_ROOT",
    "DEFAULT_CONTEXT_INPUT_DIRECTORY",
    "DEFAULT_AUDIT_DIRECTORY",
    "DEFAULT_INPUT_DIRECTORY",
    "CONTEXT_INPUT_ARTIFACT_VERSION",
    "INPUT_ARTIFACT_VERSION",
    "MAX_PROVIDER_CALLS",
    "MAX_SELECTED_PAGES",
    "GUARD_VALIDATION_ARTIFACT_VERSION",
    "GUARD_VALIDATION_ARTIFACT_NAMESPACE",
    "DEFAULT_GUARD_VALIDATION_ARTIFACT_DIRECTORY",
    "GUARD_VALIDATION_MAX_SELECTED_PAGES",
    "GUARD_VALIDATION_MAX_PROVIDER_CALLS",
    "GUARD_VALIDATION_PER_PAGE_PROVIDER_CALL_LIMIT",
    "GUARD_VALIDATION_MAX_RETRIES",
    "GUARD_VALIDATION_SDK_CALLS",
    "GUARD_VALIDATION_PROMPT_VERSION",
    "GUARD_VALIDATION_TAXONOMY_VERSION",
    "GUARD_VALIDATION_SYSTEM_PROMPT",
    "GUARD_VALIDATION_PROMPT",
    "GUARD_VALIDATION_SUBSET",
    "GUARD_VALIDATION_PAGE_REFS",
    "GUARD_VALIDATION_ERROR_TAXONOMY",
    "GUARD_VALIDATION_SUBSET_SHA256",
    "GUARDED_ARTIFACT_VERSION",
    "GUARDED_ARTIFACT_NAMESPACE",
    "DEFAULT_GUARDED_ARTIFACT_DIRECTORY",
    "GUARDED_MAX_SELECTED_PAGES",
    "GUARDED_MAX_PROVIDER_CALLS",
    "GUARDED_PER_PAGE_PROVIDER_CALL_LIMIT",
    "GUARDED_MAX_RETRIES",
    "GUARDED_SDK_CALLS",
    "GUARDED_PROMPT_VERSION",
    "GUARDED_TAXONOMY_VERSION",
    "GUARDED_SYSTEM_PROMPT",
    "AUTHORITY_BOUND_ARTIFACT_VERSION",
    "AUTHORITY_BOUND_ARTIFACT_NAMESPACE",
    "DEFAULT_AUTHORITY_BOUND_ARTIFACT_DIRECTORY",
    "AUTHORITY_BOUND_MAX_SELECTED_PAGES",
    "AUTHORITY_BOUND_MAX_TOTAL_CALLS",
    "AUTHORITY_BOUND_MAX_PROVIDER_CALLS",
    "AUTHORITY_BOUND_PER_PAGE_PROVIDER_CALL_LIMIT",
    "AUTHORITY_BOUND_MAX_RETRIES",
    "AUTHORITY_BOUND_SUPPLEMENT_CALLS",
    "AUTHORITY_BOUND_SUPPLEMENT_PAGES",
    "AUTHORITY_BOUND_HEALTH_CALLS",
    "AUTHORITY_BOUND_SDK_CALLS",
    "AUTHORITY_BOUND_PROMPT_VERSION",
    "AUTHORITY_BOUND_TAXONOMY_VERSION",
    "AUTHORITY_BOUND_SYSTEM_PROMPT",
    "AUTHORITY_BOUND_PROMPT",
    "DEFAULT_AUTHORITY_BOUND_AUDIT_DIRECTORY",
    "AUTHORITY_BOUND_AUDIT_FILENAME",
    "AUTHORITY_BOUND_AUDITED_PAGE_REFS",
    "AUTHORITY_BOUND_PROJECTION_PAGE_REFS",
    "AUTHORITY_BOUND_PAGE_REFS",
    "AUTHORITY_BOUND_AUDIT_SCOPE_REF",
    "AUTHORITY_BOUND_SCOPE_REF",
    "AUTHORITY_BOUND_SELECTION_SHA256",
    "AUTHORITY_BOUND_CONTEXT_INPUT_SHA256",
    "AUTHORITY_BOUND_PROJECTION_CODE_SHA256",
    "AUTHORITY_BOUND_AUDIT_SUMMARY_SHA256",
    "AUTHORITY_BOUND_HUMAN_AUDIT_SHA256",
    "AUTHORITY_BOUND_CANONICAL_MAPPING_VERSION",
    "AUTHORITY_BOUND_CANONICAL_MAPPING",
    "AUTHORITY_BOUND_CANONICAL_MAP",
    "AUTHORITY_BOUND_PAGE_MAPPING",
    "AUTHORITY_BOUND_CANONICAL_PAGE_MAPPING",
    "AUTHORITY_BOUND_CANONICAL_MAPPING_SHA256",
    "AUTHORITY_BOUND_SCOPE",
    "AUTHORITY_BOUND_SCOPE_SHA256",
    "AUTHORITY_BOUND_SETTINGS_SHA256",
    "AUTHORITY_BOUND_PROMPT_SHA256",
    "AUTHORITY_BOUND_CODE_SHA256",
    "TOPIC_GUIDED_AUTHORIZATION_ID",
    "AUTHORIZATION_ID_TOPIC_GUIDED",
    "AUTHORIZATION_ID_TOPIC_GUIDED_20260830",
    "TOPIC_GUIDED_AUTHORIZATION_ID_20260830",
    "STRATIFIED_CURRENT_STAGE_A_V3_PILOT_TOPIC_GUIDED_20260830",
    "TOPIC_GUIDED_ARTIFACT_VERSION",
    "TOPIC_GUIDED_ARTIFACT_NAMESPACE",
    "TOPIC_GUIDED_NAMESPACE",
    "DEFAULT_TOPIC_GUIDED_ARTIFACT_DIRECTORY",
    "DEFAULT_TOPIC_GUIDED_REVIEW_DIRECTORY",
    "DEFAULT_TOPIC_GUIDED_AUDIT_DIRECTORY",
    "TOPIC_GUIDED_MAX_SELECTED_PAGES",
    "TOPIC_GUIDED_MAX_TOTAL_CALLS",
    "TOPIC_GUIDED_MAX_PROVIDER_CALLS",
    "TOPIC_GUIDED_PER_PAGE_PROVIDER_CALL_LIMIT",
    "TOPIC_GUIDED_MAX_RETRIES",
    "TOPIC_GUIDED_SUPPLEMENT_CALLS",
    "TOPIC_GUIDED_SUPPLEMENT_PAGES",
    "TOPIC_GUIDED_HEALTH_CALLS",
    "TOPIC_GUIDED_SDK_CALLS",
    "TOPIC_GUIDED_PROMPT_VERSION",
    "TOPIC_GUIDED_TAXONOMY_VERSION",
    "TOPIC_GUIDED_GUIDANCE_VERSION",
    "TOPIC_GUIDED_GROUPING_HINTS_VERSION",
    "TOPIC_GUIDED_TOKEN_CALIBRATION_VERSION",
    "TOPIC_GUIDED_CONTEXT_SELECTION_VERSION",
    "TOPIC_GUIDED_CONTEXT_TELEMETRY_VERSION",
    "TOPIC_GUIDED_CONTEXT_SELECTION_PRIORITY",
    "TOPIC_GUIDED_REVIEW_FILENAME",
    "TOPIC_GUIDED_REVIEW_SCHEMA",
    "TOPIC_GUIDED_REVIEW_STATUS",
    "TOPIC_GUIDED_REVIEW_SHA256",
    "TOPIC_GUIDED_MANIFEST_SHA256",
    "TOPIC_GUIDED_CONTEXT_MANIFEST_SHA256",
    "TOPIC_GUIDED_DEVELOPMENT_SOURCE_SHA256",
    "TOPIC_GUIDED_PROTOCOL_SOURCE_SHA256",
    "TOPIC_GUIDED_PREVIOUS_REVIEW_SHA256",
    "TOPIC_GUIDED_CANONICAL_MAPPING_VERSION",
    "TOPIC_GUIDED_CANONICAL_MAPPING",
    "TOPIC_GUIDED_CANONICAL_MAP",
    "TOPIC_GUIDED_PAGE_MAPPING",
    "TOPIC_GUIDED_CANONICAL_PAGE_MAPPING",
    "TOPIC_GUIDED_CANONICAL_MAPPING_SHA256",
    "TOPIC_GUIDED_SCOPE",
    "TOPIC_GUIDED_SCOPE_SHA256",
    "TOPIC_GUIDED_SETTINGS_SHA256",
    "TOPIC_GUIDED_AUTHORITY_SCOPE_REF",
    "TOPIC_GUIDED_AUTHORITY_PAGE_REFS",
    "TOPIC_GUIDED_AUTHORITY_PROJECTION_PAGE_REFS",
    "TOPIC_GUIDED_PROMPT_SHA256",
    "TOPIC_GUIDED_GUIDANCE_SHA256",
    "TOPIC_GUIDED_GROUPING_HINTS_SHA256",
    "TOPIC_GUIDED_CODE_SHA256",
    "TOPIC_GUIDED_SYSTEM_PROMPT",
    "TOPIC_GUIDED_PROMPT",
    "TOPIC_GUIDED_EXPECTED_PRIMARY_COUNTS",
    "TOPIC_GUIDED_EXPECTED_G_HINT_COUNTS",
    "TOPIC_GUIDED_EXPECTED_G_RELATIONS",
    "TOPIC_GUIDED_RELATION_FAMILIES",
    "TOPIC_GUIDED_REVIEW_PAGE_REFS",
    "ERROR_TAXONOMY_VERSION",
    "KNOWN_VALIDATION_ERROR_CODES",
    "OUTPUT_FILENAMES",
    "PageRun",
    "REPORT_SCHEMA_VERSION",
    "RUNNER_SCHEMA_VERSION",
    "context_recovery_snapshot",
    "offline_topic_guided_request_report",
    "topic_guided_offline_preflight_report",
    "analyze_compact_stage_a_selection",
    "validation_categories_for_error_code",
    "error_category_for_code",
    "validate_guard_validation_output",
    "validate_guard_output",
    "validate_guard_response",
    "run_compact_stage_a_development_pilot_v3",
    "run_compact_stage_a_development_v3",
    "run_stage_a_development_pilot_v3",
]
