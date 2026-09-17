"""Durable, body-free authorization ledger for provider call budgets.

This module is intentionally independent from the semantic runners.  A runner
may use it as a *pre-provider* guard:

    ledger = CallAuthorizationLedger(...)
    reservation = ledger.reserve(request, unit_ref="opaque-page-ref")
    ledger.mark_started(reservation)
    try:
        result = provider(...)
    except Exception:
        ledger.mark_failed(reservation, error_code="provider_error")
        raise
    else:
        ledger.mark_complete(reservation, input_tokens=..., output_tokens=...)

``reserve`` is the point at which a call is consumed.  It performs an atomic
SQLite transaction before a provider can be called.  A crash after reservation
therefore leaves a durable ``reserved`` row and cannot be used to reclaim the
call by starting a new process or writing a new output directory.  The durable
ledger records hashes, enums, counters and controlled metadata only; request
bodies, provider responses, exception messages and credentials never enter the
database.

The module does not construct or call a provider and does not read any project
artifact.  Callers must supply a stable ledger path (or use
``ledger_path_for``/``for_authorization`` to derive one from an authority
root).  The same ``authorization_id`` is bound to settings, protocol, model,
provider, input lineage and scope.  Reopening it with a changed binding is a
hard error rather than a new budget.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple, Union

from .compact_stage_a_protocol_v3 import KNOWN_VALIDATION_ERROR_CODES


MODULE_SCHEMA_VERSION = "persistent_call_budget_v1"
LEDGER_SCHEMA_VERSION = "call_authorization_ledger_v1"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_AUTHORIZATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
_SAFE_ERROR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
_FORBIDDEN_PATH_PARTS = frozenset({"frozen", "frozen_test", "frozen-test"})

# These names are used only to reject accidental request/body persistence in
# optional metadata.  The actual request is hashed and never serialized.
_BODY_KEYS = frozenset(
    {
        "body",
        "content",
        "message",
        "messages",
        "prompt",
        "request",
        "response",
        "raw",
        "text",
        "user_input",
    }
)

_SAFE_ERROR_CODES = frozenset(
    {
        "authorization_call_budget_exhausted",
        "input_token_limit_exceeded",
        "output_token_limit_exceeded",
        "provider_error",
        "provider_invalid_json",
        "provider_timeout",
        "provider_pending",
        "provider_abandoned",
        "provider_protocol_error",
        "provider_network_error",
        "provider_authentication_error",
        "provider_rate_limited",
        "provider_unavailable",
        "schema_validation_failed",
        "unknown",
        # Readable semantic bundle contract codes.  They are intentionally
        # enumerated here so the durable ledger preserves the exact,
        # body-free validator taxonomy instead of collapsing every semantic
        # failure to provider_error.
        "request_primary_messages_empty",
        "semantic_invalid_json",
        "semantic_output_not_object",
        "semantic_missing_topics",
        "semantic_missing_people",
        "semantic_missing_objects",
        "semantic_missing_states",
        "semantic_missing_overall_uncertainties",
        "semantic_topics_empty",
        "semantic_core_primary_not_exactly_one",
        "semantic_core_primary_not_exactly_once",
        "semantic_alias_out_of_scope",
        "semantic_primary_context_overlap",
        "semantic_known_topic_without_evidence",
        "semantic_known_entity_without_evidence",
        "semantic_known_state_without_evidence",
        "semantic_topic_missing_field",
        "semantic_topic_not_object",
        "semantic_topic_id_not_string",
        "semantic_topic_label_not_string",
        "semantic_primary_aliases_invalid",
        "semantic_primary_aliases_invalid_duplicate",
        "semantic_context_aliases_invalid",
        "semantic_context_aliases_invalid_duplicate",
        "semantic_topic_uncertainty_not_string",
        "semantic_evidence_aliases_invalid",
        "semantic_evidence_aliases_invalid_duplicate",
        "semantic_entity_name_not_string",
        "semantic_entity_role_not_string",
        "semantic_entity_evidence_invalid",
        "semantic_entity_evidence_invalid_duplicate",
        "semantic_people_not_object",
        "semantic_people_missing_field",
        "semantic_objects_not_object",
        "semantic_objects_missing_field",
        "semantic_states_not_object",
        "semantic_states_missing_field",
        "semantic_state_subject_not_string",
        "semantic_state_object_not_string",
        "semantic_state_value_not_string",
        "semantic_state_modality_not_string",
        "semantic_state_evidence_invalid",
        "semantic_state_evidence_invalid_duplicate",
        "semantic_uncertainties_invalid",
        "semantic_uncertainties_invalid_duplicate",
        "settings_mutated",
    }
)
# Compact Stage-A validation failures are safe machine codes too.  Keep them
# in the durable ledger instead of degrading them to ``provider_error``; the
# runner still controls which codes can originate from an arbitrary adapter.
_SAFE_ERROR_CODES = frozenset(set(_SAFE_ERROR_CODES) | set(KNOWN_VALIDATION_ERROR_CODES))


class CallBudgetError(RuntimeError):
    """Base class for safe, body-free authorization errors."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = str(code)
        super().__init__(message or self.code)


class CallBudgetExceeded(CallBudgetError):
    """Raised when no provider call remains for an authorization."""

    def __init__(self, authorization_id: str, *, limit: int, used: int) -> None:
        self.authorization_id = authorization_id
        self.limit = int(limit)
        self.used = int(used)
        super().__init__("authorization_call_budget_exhausted")


class ReservationRejected(CallBudgetError):
    """Raised for a pre-provider request that cannot be reserved."""


class AuthorizationBindingMismatch(CallBudgetError):
    """Raised when a stable authorization is reopened with changed context."""


class ReservationStateError(CallBudgetError):
    """Raised for an invalid or repeated reservation state transition."""


class LedgerPathError(CallBudgetError):
    """Raised for an unsafe or unusable ledger path."""


def _canonical_json(value: Any) -> str:
    """Canonicalize only in memory; never use this for persisted raw input."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def _json_default(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError("unsupported_json_value")


def _sha256(value: Any) -> str:
    if isinstance(value, bytes):
        data = value
    elif isinstance(value, str):
        data = value.encode("utf-8")
    else:
        data = _canonical_json(value).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _validate_hash(value: Any, *, field: str, allow_empty: bool = False) -> str:
    text = "" if value is None else str(value).strip().lower()
    if allow_empty and not text:
        return ""
    if not _HEX64.fullmatch(text):
        raise ValueError("%s_must_be_sha256" % field)
    return text


def _validate_token(value: Any, *, field: str, default: str = "unknown") -> str:
    text = default if value is None else str(value)
    if not text:
        text = default
    if not _SAFE_TOKEN.fullmatch(text):
        raise ValueError("invalid_%s" % field)
    return text


def _validate_authorization_id(value: Any) -> str:
    text = "" if value is None else str(value)
    if not _AUTHORIZATION_ID.fullmatch(text):
        raise ValueError("invalid_authorization_id")
    return text


def _validate_scope(scope: Any) -> str:
    """Return a scope digest, never the supplied scope itself."""

    if scope is None:
        value: Any = {}
    elif isinstance(scope, Mapping):
        # Reject an accidental body-shaped scope rather than storing or
        # hashing an unbounded arbitrary object.  Values are still only hashed.
        for key in scope:
            if str(key).casefold() in _BODY_KEYS:
                raise ValueError("scope_contains_body_key")
        value = {str(key): scope[key] for key in sorted(scope, key=lambda item: str(item))}
    elif isinstance(scope, (str, int, float, bool)):
        value = str(scope)
    else:
        raise ValueError("invalid_scope")
    return _sha256(value)


def _validate_settings_hash(settings_sha256: Any = None, settings: Any = None) -> str:
    if settings_sha256 is not None:
        return _validate_hash(settings_sha256, field="settings_sha256", allow_empty=True)
    if settings is None:
        return ""
    # Settings are used only to derive an in-memory fingerprint.  In
    # particular, an api_key is never written to SQLite or an exported ledger.
    return _sha256(settings)


def _safe_error_code(value: Any) -> str:
    text = "provider_error" if value is None else str(value).strip()
    if not _SAFE_ERROR.fullmatch(text) or text not in _SAFE_ERROR_CODES:
        return "provider_error"
    return text


def _safe_path(path: Union[str, os.PathLike[str]]) -> Path:
    raw = Path(path)
    if str(raw) in {"", "."}:
        raise LedgerPathError("ledger_path_required")
    resolved = raw.expanduser().resolve()
    if any(part.casefold() in _FORBIDDEN_PATH_PARTS for part in resolved.parts):
        raise LedgerPathError("ledger_path_forbidden_scope")
    return resolved


def _safe_int(value: Any, *, field: str, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid_%s" % field) from exc
    if result < minimum or result > maximum:
        raise ValueError("invalid_%s" % field)
    return result


@dataclass(frozen=True)
class AuthorizationBinding:
    """Stable, body-free identity of one authorized run."""

    authorization_id: str
    max_calls: int
    provider: str
    model: str
    protocol: str
    settings_sha256: str
    scope_sha256: str
    input_sha256: str
    artifact_namespace: str

    @property
    def fingerprint(self) -> str:
        return _sha256(self.to_dict())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": MODULE_SCHEMA_VERSION,
            "authorization_id": self.authorization_id,
            "max_calls": self.max_calls,
            "provider": self.provider,
            "model": self.model,
            "protocol": self.protocol,
            "settings_sha256": self.settings_sha256,
            "scope_sha256": self.scope_sha256,
            "input_sha256": self.input_sha256,
            "artifact_namespace": self.artifact_namespace,
        }


@dataclass(frozen=True)
class CallReservation:
    """Opaque handle returned only after an atomic budget reservation."""

    authorization_id: str
    reservation_id: str
    ordinal: int
    request_sha256: str
    unit_ref_sha256: str
    attempt: int
    provider: str
    model: str
    protocol: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": MODULE_SCHEMA_VERSION,
            "authorization_id": self.authorization_id,
            "reservation_id": self.reservation_id,
            "ordinal": self.ordinal,
            "request_sha256": self.request_sha256,
            "unit_ref_sha256": self.unit_ref_sha256,
            "attempt": self.attempt,
            "provider": self.provider,
            "model": self.model,
            "protocol": self.protocol,
        }


# Descriptive aliases for callers that use the terminology "guard" or
# "authorization budget".
AuthorizationReservation = CallReservation
PersistentCallAuthorizationLedger = None  # assigned after the class definition
GlobalCallBudgetGuard = None


def ledger_path_for(root: Union[str, os.PathLike[str]], authorization_id: str) -> Path:
    """Derive a stable per-authorization ledger path from an authority root.

    This is the preferred construction for rerunnable jobs: output artifact
    directories are not part of the path, and a second output directory cannot
    silently create a fresh counter for the same authorization id.
    """

    root_path = _safe_path(root)
    auth = _validate_authorization_id(authorization_id)
    root_path.mkdir(parents=True, exist_ok=True)
    return root_path / ("authorization-%s.sqlite3" % _sha256(auth)[:32])


class CallAuthorizationLedger:
    """SQLite-backed atomic provider-call authorization ledger.

    The class is safe to reopen from another process.  All mutations use
    ``BEGIN IMMEDIATE`` so the increment and reservation row are one atomic
    transaction.  A reservation is never released: ``reserved``, ``started``,
    ``complete``, ``failed``, ``pending`` and ``abandoned`` all count against
    the same call limit.
    """

    _TABLES_SQL = """
    CREATE TABLE IF NOT EXISTS authorizations (
        authorization_id TEXT PRIMARY KEY,
        binding_sha256 TEXT NOT NULL,
        max_calls INTEGER NOT NULL CHECK (max_calls >= 0),
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        protocol TEXT NOT NULL,
        settings_sha256 TEXT NOT NULL,
        scope_sha256 TEXT NOT NULL,
        input_sha256 TEXT NOT NULL,
        artifact_namespace TEXT NOT NULL,
        created_unix_ms INTEGER NOT NULL,
        calls_reserved INTEGER NOT NULL DEFAULT 0 CHECK (calls_reserved >= 0)
    );
    CREATE TABLE IF NOT EXISTS reservations (
        reservation_id TEXT PRIMARY KEY,
        authorization_id TEXT NOT NULL REFERENCES authorizations(authorization_id),
        ordinal INTEGER NOT NULL,
        request_sha256 TEXT NOT NULL,
        unit_ref_sha256 TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        protocol TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('reserved','started','complete','failed','pending','abandoned')),
        error_code TEXT,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        latency_ms REAL NOT NULL DEFAULT 0,
        created_unix_ms INTEGER NOT NULL,
        updated_unix_ms INTEGER NOT NULL,
        UNIQUE (authorization_id, ordinal)
    );
    CREATE TABLE IF NOT EXISTS rejections (
        rejection_id INTEGER PRIMARY KEY AUTOINCREMENT,
        authorization_id TEXT NOT NULL,
        request_sha256 TEXT NOT NULL,
        unit_ref_sha256 TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        code TEXT NOT NULL,
        created_unix_ms INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS reservations_authorization_idx
        ON reservations(authorization_id, ordinal);
    CREATE INDEX IF NOT EXISTS rejections_authorization_idx
        ON rejections(authorization_id, rejection_id);
    """

    _VALID_STATUSES = frozenset({"reserved", "started", "complete", "failed", "pending", "abandoned"})

    def __init__(
        self,
        ledger_path: Union[str, os.PathLike[str]],
        *,
        authorization_id: str,
        max_calls: Optional[int] = None,
        max_provider_calls: Optional[int] = None,
        provider: str = "unknown",
        model: str = "unknown",
        protocol: str = "unknown",
        settings_sha256: Any = None,
        settings: Any = None,
        scope: Any = None,
        input_sha256: Any = None,
        artifact_namespace: Any = "",
        max_input_tokens: Optional[int] = None,
    ) -> None:
        if max_calls is None:
            max_calls = max_provider_calls
        if max_calls is None:
            raise ValueError("max_calls_required")
        self.path = _safe_path(ledger_path)
        self.binding = AuthorizationBinding(
            authorization_id=_validate_authorization_id(authorization_id),
            max_calls=_safe_int(max_calls, field="max_calls"),
            provider=_validate_token(provider, field="provider"),
            model=_validate_token(model, field="model"),
            protocol=_validate_token(protocol, field="protocol"),
            settings_sha256=_validate_settings_hash(settings_sha256, settings),
            scope_sha256=_validate_scope(scope),
            input_sha256=(
                _validate_hash(input_sha256, field="input_sha256", allow_empty=True)
                if input_sha256 is not None
                else ""
            ),
            artifact_namespace=_validate_token(artifact_namespace or "unknown", field="artifact_namespace", default="unknown"),
        )
        self.max_input_tokens = None if max_input_tokens is None else _safe_int(
            max_input_tokens, field="max_input_tokens", minimum=1
        )
        self._local_lock = threading.RLock()
        self._initialize()

    @classmethod
    def for_authorization(
        cls,
        authority_root: Union[str, os.PathLike[str]],
        **kwargs: Any,
    ) -> "CallAuthorizationLedger":
        """Open the deterministic ledger for an authorization id."""

        authorization_id = kwargs.get("authorization_id")
        if authorization_id is None:
            raise ValueError("authorization_id_required")
        path = ledger_path_for(authority_root, str(authorization_id))
        return cls(path, **kwargs)

    open_for_authorization = for_authorization

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        # Use the rollback journal rather than WAL: a committed reservation is
        # durable in the main ledger file and does not depend on a caller
        # copying a sidecar ``-wal`` file along with it.  BEGIN IMMEDIATE still
        # gives us the required cross-process atomic reservation.
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(self._TABLES_SQL)
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM authorizations WHERE authorization_id = ?",
                    (self.binding.authorization_id,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO authorizations (
                            authorization_id, binding_sha256, max_calls, provider,
                            model, protocol, settings_sha256, scope_sha256,
                            input_sha256, artifact_namespace, created_unix_ms,
                            calls_reserved
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                        """,
                        (
                            self.binding.authorization_id,
                            self.binding.fingerprint,
                            self.binding.max_calls,
                            self.binding.provider,
                            self.binding.model,
                            self.binding.protocol,
                            self.binding.settings_sha256,
                            self.binding.scope_sha256,
                            self.binding.input_sha256,
                            self.binding.artifact_namespace,
                            int(time.time() * 1000),
                        ),
                    )
                else:
                    self._assert_row_matches_binding(row)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _assert_row_matches_binding(self, row: Mapping[str, Any]) -> None:
        stored = {
            "authorization_id": str(row["authorization_id"]),
            "max_calls": int(row["max_calls"]),
            "provider": str(row["provider"]),
            "model": str(row["model"]),
            "protocol": str(row["protocol"]),
            "settings_sha256": str(row["settings_sha256"]),
            "scope_sha256": str(row["scope_sha256"]),
            "input_sha256": str(row["input_sha256"]),
            "artifact_namespace": str(row["artifact_namespace"]),
        }
        expected = {
            key: value
            for key, value in self.binding.to_dict().items()
            if key != "schema_version"
        }
        if stored != expected:
            raise AuthorizationBindingMismatch("authorization_binding_mismatch")
        if str(row["binding_sha256"]) != self.binding.fingerprint:
            raise AuthorizationBindingMismatch("authorization_binding_hash_mismatch")

    @staticmethod
    def _request_sha(request: Any = None, request_sha256: Any = None) -> str:
        if request_sha256 is not None:
            return _validate_hash(request_sha256, field="request_sha256")
        if request is None:
            raise ValueError("request_or_request_sha256_required")
        return _sha256(request)

    @staticmethod
    def _unit_sha(unit_ref: Any = None, unit_ref_sha256: Any = None) -> str:
        if unit_ref_sha256 is not None:
            return _validate_hash(unit_ref_sha256, field="unit_ref_sha256")
        if unit_ref is None:
            return _sha256("")
        return _sha256(unit_ref)

    def _check_request_binding(
        self,
        *,
        provider: Optional[str],
        model: Optional[str],
        protocol: Optional[str],
        settings_sha256: Any,
        scope: Any,
        input_sha256: Any,
        artifact_namespace: Any,
    ) -> None:
        expected = self.binding
        actual_provider = expected.provider if provider is None else _validate_token(provider, field="provider")
        actual_model = expected.model if model is None else _validate_token(model, field="model")
        actual_protocol = expected.protocol if protocol is None else _validate_token(protocol, field="protocol")
        actual_settings = expected.settings_sha256
        if settings_sha256 is not None:
            actual_settings = _validate_hash(settings_sha256, field="settings_sha256", allow_empty=True)
        actual_scope = expected.scope_sha256 if scope is None else _validate_scope(scope)
        actual_input = expected.input_sha256
        if input_sha256 is not None:
            actual_input = _validate_hash(input_sha256, field="input_sha256", allow_empty=True)
        actual_artifact = expected.artifact_namespace
        if artifact_namespace is not None:
            actual_artifact = _validate_token(artifact_namespace or "unknown", field="artifact_namespace", default="unknown")
        if (
            actual_provider != expected.provider
            or actual_model != expected.model
            or actual_protocol != expected.protocol
            or actual_settings != expected.settings_sha256
            or actual_scope != expected.scope_sha256
            or actual_input != expected.input_sha256
            or actual_artifact != expected.artifact_namespace
        ):
            raise AuthorizationBindingMismatch("request_binding_mismatch")

    def reserve(
        self,
        request: Any = None,
        *,
        request_sha256: Any = None,
        unit_ref: Any = None,
        unit_ref_sha256: Any = None,
        attempt: int = 0,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        protocol: Optional[str] = None,
        settings_sha256: Any = None,
        settings: Any = None,
        scope: Any = None,
        input_sha256: Any = None,
        artifact_namespace: Any = None,
        input_tokens_estimate: int = 0,
    ) -> CallReservation:
        """Atomically consume one call slot before any provider invocation.

        ``request`` is accepted for convenience but is hashed in memory only.
        If the request is too large for an optional input limit, a rejection is
        persisted without consuming a provider-call slot.  All exceptions after
        a successful return must be finalized with ``mark_failed`` or another
        terminal transition; the slot is already consumed either way.
        """

        request_hash = self._request_sha(request, request_sha256)
        unit_hash = self._unit_sha(unit_ref, unit_ref_sha256)
        attempt_value = _safe_int(attempt, field="attempt")
        input_estimate = _safe_int(input_tokens_estimate, field="input_tokens_estimate")
        effective_scope = scope
        if effective_scope is None and isinstance(request, Mapping):
            if isinstance(request.get("scope"), Mapping):
                effective_scope = request.get("scope")
            elif "account_id" in request and "chat_id" in request:
                effective_scope = {
                    "account_id": request.get("account_id"),
                    "chat_id": request.get("chat_id"),
                }
        effective_input_sha = input_sha256
        if effective_input_sha is None and isinstance(request, Mapping):
            if request.get("input_sha256") is not None:
                effective_input_sha = request.get("input_sha256")
        effective_artifact = artifact_namespace
        if effective_artifact is None and isinstance(request, Mapping):
            if request.get("artifact_namespace") is not None:
                effective_artifact = request.get("artifact_namespace")
        effective_settings = settings_sha256
        if effective_settings is None and settings is not None:
            effective_settings = _validate_settings_hash(settings=settings)
        self._check_request_binding(
            provider=provider,
            model=model,
            protocol=protocol,
            settings_sha256=effective_settings,
            scope=effective_scope,
            input_sha256=effective_input_sha,
            artifact_namespace=effective_artifact,
        )
        if self.max_input_tokens is not None and input_estimate > self.max_input_tokens:
            self._record_rejection(request_hash, unit_hash, attempt_value, "input_token_limit_exceeded")
            raise ReservationRejected("input_token_limit_exceeded")

        with self._local_lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    row = connection.execute(
                        "SELECT * FROM authorizations WHERE authorization_id = ?",
                        (self.binding.authorization_id,),
                    ).fetchone()
                    if row is None:
                        raise AuthorizationBindingMismatch("authorization_missing")
                    self._assert_row_matches_binding(row)
                    used = int(row["calls_reserved"])
                    limit = int(row["max_calls"])
                    if used >= limit:
                        self._insert_rejection(
                            connection,
                            request_hash,
                            unit_hash,
                            attempt_value,
                            "authorization_call_budget_exhausted",
                        )
                        connection.commit()
                        raise CallBudgetExceeded(self.binding.authorization_id, limit=limit, used=used)
                    ordinal = used + 1
                    reservation_id = "%s-%06d-%s" % (
                        self.binding.authorization_id,
                        ordinal,
                        secrets.token_hex(4),
                    )
                    now = int(time.time() * 1000)
                    connection.execute(
                        """
                        INSERT INTO reservations (
                            reservation_id, authorization_id, ordinal,
                            request_sha256, unit_ref_sha256, attempt,
                            provider, model, protocol, status, error_code,
                            input_tokens, output_tokens, latency_ms,
                            created_unix_ms, updated_unix_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', NULL, 0, 0, 0, ?, ?)
                        """,
                        (
                            reservation_id,
                            self.binding.authorization_id,
                            ordinal,
                            request_hash,
                            unit_hash,
                            attempt_value,
                            self.binding.provider,
                            self.binding.model,
                            self.binding.protocol,
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        "UPDATE authorizations SET calls_reserved = ? WHERE authorization_id = ?",
                        (ordinal, self.binding.authorization_id),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        return CallReservation(
            authorization_id=self.binding.authorization_id,
            reservation_id=reservation_id,
            ordinal=ordinal,
            request_sha256=request_hash,
            unit_ref_sha256=unit_hash,
            attempt=attempt_value,
            provider=self.binding.provider,
            model=self.binding.model,
            protocol=self.binding.protocol,
        )

    def try_reserve(self, *args: Any, **kwargs: Any) -> Optional[CallReservation]:
        """Return ``None`` for an exhausted/preflight-rejected budget."""

        try:
            return self.reserve(*args, **kwargs)
        except (CallBudgetExceeded, ReservationRejected):
            return None

    # Explicit aliases make the integration boundary readable in runners
    # without duplicating the transactional implementation.
    reserve_call = reserve

    def _record_rejection(self, request_hash: str, unit_hash: str, attempt: int, code: str) -> None:
        with self._local_lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    self._insert_rejection(connection, request_hash, unit_hash, attempt, code)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

    def _insert_rejection(
        self,
        connection: sqlite3.Connection,
        request_hash: str,
        unit_hash: str,
        attempt: int,
        code: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO rejections (
                authorization_id, request_sha256, unit_ref_sha256,
                attempt, code, created_unix_ms
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                self.binding.authorization_id,
                request_hash,
                unit_hash,
                attempt,
                _safe_error_code(code),
                int(time.time() * 1000),
            ),
        )

    def _reservation_row(self, connection: sqlite3.Connection, reservation: CallReservation) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM reservations WHERE reservation_id = ? AND authorization_id = ?",
            (reservation.reservation_id, self.binding.authorization_id),
        ).fetchone()
        if row is None:
            raise ReservationStateError("reservation_not_found")
        if int(row["ordinal"]) != int(reservation.ordinal) or str(row["request_sha256"]) != reservation.request_sha256:
            raise ReservationStateError("reservation_handle_mismatch")
        return row

    def mark_started(self, reservation: CallReservation) -> None:
        """Record that the provider call was entered; this never changes usage."""

        self._transition(reservation, from_statuses=("reserved",), to_status="started")

    record_started = mark_started

    def mark_complete(
        self,
        reservation: CallReservation,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: float = 0.0,
    ) -> None:
        self._transition(
            reservation,
            from_statuses=("reserved", "started"),
            to_status="complete",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
        )

    record_success = mark_complete

    def mark_failed(
        self,
        reservation: CallReservation,
        *,
        error_code: Any = "provider_error",
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: float = 0.0,
    ) -> None:
        self._transition(
            reservation,
            from_statuses=("reserved", "started"),
            to_status="failed",
            error_code=_safe_error_code(error_code),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
        )

    record_failure = mark_failed

    def mark_pending(
        self,
        reservation: CallReservation,
        *,
        error_code: Any = "provider_pending",
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: float = 0.0,
    ) -> None:
        self._transition(
            reservation,
            from_statuses=("reserved", "started"),
            to_status="pending",
            error_code=_safe_error_code(error_code),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
        )

    record_pending = mark_pending

    def mark_abandoned(self, reservation: CallReservation, *, error_code: Any = "provider_abandoned") -> None:
        self._transition(
            reservation,
            from_statuses=("reserved", "started"),
            to_status="abandoned",
            error_code=_safe_error_code(error_code),
        )

    record_abandoned = mark_abandoned

    def _transition(
        self,
        reservation: CallReservation,
        *,
        from_statuses: Sequence[str],
        to_status: str,
        error_code: Optional[str] = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: float = 0.0,
    ) -> None:
        input_value = _safe_int(input_tokens, field="input_tokens")
        output_value = _safe_int(output_tokens, field="output_tokens")
        try:
            latency_value = float(latency_ms)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("invalid_latency_ms") from exc
        if latency_value < 0 or latency_value > 2**63:
            raise ValueError("invalid_latency_ms")
        with self._local_lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    row = self._reservation_row(connection, reservation)
                    status = str(row["status"])
                    if status not in from_statuses:
                        raise ReservationStateError("invalid_reservation_transition")
                    now = int(time.time() * 1000)
                    connection.execute(
                        """
                        UPDATE reservations
                        SET status = ?, error_code = ?, input_tokens = ?,
                            output_tokens = ?, latency_ms = ?, updated_unix_ms = ?
                        WHERE reservation_id = ? AND authorization_id = ?
                        """,
                        (
                            to_status,
                            error_code,
                            input_value,
                            output_value,
                            latency_value,
                            now,
                            reservation.reservation_id,
                            self.binding.authorization_id,
                        ),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

    @property
    def calls_used(self) -> int:
        return int(self._authorization_row()["calls_reserved"])

    @property
    def authorization_id(self) -> str:
        return self.binding.authorization_id

    @property
    def ledger_path(self) -> Path:
        return self.path

    @property
    def used(self) -> int:
        return self.calls_used

    @property
    def remaining(self) -> int:
        return self.calls_remaining

    @property
    def calls_remaining(self) -> int:
        row = self._authorization_row()
        return max(0, int(row["max_calls"]) - int(row["calls_reserved"]))

    @property
    def max_calls(self) -> int:
        return int(self._authorization_row()["max_calls"])

    def _authorization_row(self) -> sqlite3.Row:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM authorizations WHERE authorization_id = ?",
                (self.binding.authorization_id,),
            ).fetchone()
        if row is None:
            raise AuthorizationBindingMismatch("authorization_missing")
        self._assert_row_matches_binding(row)
        return row

    def records(self) -> Tuple[Dict[str, Any], ...]:
        """Return body-free reservation rows in ordinal order."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT reservation_id, authorization_id, ordinal,
                       request_sha256, unit_ref_sha256, attempt,
                       provider, model, protocol, status, error_code,
                       input_tokens, output_tokens, latency_ms
                FROM reservations WHERE authorization_id = ? ORDER BY ordinal
                """,
                (self.binding.authorization_id,),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def rejections(self) -> Tuple[Dict[str, Any], ...]:
        """Return body-free pre-provider rejection rows."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT rejection_id, authorization_id, request_sha256,
                       unit_ref_sha256, attempt, code
                FROM rejections WHERE authorization_id = ? ORDER BY rejection_id
                """,
                (self.binding.authorization_id,),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def snapshot(self) -> Dict[str, Any]:
        rows = self.records()
        rejection_rows = self.rejections()
        status_counts = Counter(str(row["status"]) for row in rows)
        return {
            "schema_version": MODULE_SCHEMA_VERSION,
            "ledger_schema_version": LEDGER_SCHEMA_VERSION,
            "authorization_id": self.binding.authorization_id,
            "authorization_sha256": self.binding.fingerprint,
            "binding": self.binding.to_dict(),
            "max_calls": self.max_calls,
            "calls_used": self.calls_used,
            "calls_remaining": self.calls_remaining,
            "reservation_count": len(rows),
            "status_counts": dict(sorted(status_counts.items())),
            "rejection_count": len(rejection_rows),
            "last_rejection_code": rejection_rows[-1]["code"] if rejection_rows else None,
            "ledger_rows_sha256": _sha256(rows),
            "body_free": True,
        }

    def export_jsonl(self, path: Union[str, os.PathLike[str]]) -> Path:
        """Export only the body-free reservation rows plus no raw input."""

        output = _safe_path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for row in self.records()]
        output.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
        return output

    def close(self) -> None:
        """Compatibility no-op; connections are short-lived per operation."""

        return None

    def __enter__(self) -> "CallAuthorizationLedger":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


PersistentCallAuthorizationLedger = CallAuthorizationLedger
GlobalCallBudgetGuard = CallAuthorizationLedger
CallBudgetGuard = CallAuthorizationLedger
PersistentCallBudget = CallAuthorizationLedger
AuthorizationLedger = CallAuthorizationLedger
ProviderCallAuthorizationLedger = CallAuthorizationLedger
AuthorizationBudgetExceeded = CallBudgetExceeded


__all__ = [
    "MODULE_SCHEMA_VERSION",
    "LEDGER_SCHEMA_VERSION",
    "AuthorizationBinding",
    "CallReservation",
    "AuthorizationReservation",
    "CallAuthorizationLedger",
    "PersistentCallAuthorizationLedger",
    "GlobalCallBudgetGuard",
    "CallBudgetGuard",
    "PersistentCallBudget",
    "AuthorizationLedger",
    "ProviderCallAuthorizationLedger",
    "CallBudgetError",
    "CallBudgetExceeded",
    "AuthorizationBudgetExceeded",
    "ReservationRejected",
    "AuthorizationBindingMismatch",
    "ReservationStateError",
    "LedgerPathError",
    "ledger_path_for",
]
