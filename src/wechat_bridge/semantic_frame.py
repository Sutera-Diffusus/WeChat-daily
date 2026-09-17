"""Strict ``semantic_frame_v1`` line protocol.

The parser is intentionally independent of any provider SDK.  It accepts no
semantic repairs: field order, JSON types, the top-level field set, evidence
references, and local bundle invariants are all checked before a frame can
reach the normal bundle normalizer.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .bundle_semantics import (
    BUNDLE_FIELDS,
    BUNDLE_SCHEMA_VERSION,
    CLAIM_TYPES,
    MODALITIES,
    STATES,
    UNKNOWN,
    validate_bundle,
)


SEMANTIC_FRAME_VERSION = "semantic_frame_v1"
_FENCE_LINES = {"```", "```text", "```semantic_frame_v1"}
_VALIDATION_CATEGORIES = frozenset(
    {
        "evidence_boundary",
        "evidence_reference",
        "schema_enum_or_type",
        "cross_chat_or_relation",
        "conflict",
        "other_validation",
    }
)


def _public_field_names(value: Any) -> List[str]:
    """Keep diagnostics limited to the fixed public schema field names."""

    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [str(item) for item in value if str(item) in BUNDLE_FIELDS]


def _empty_field_diagnostics() -> Dict[str, Any]:
    return {
        "missing_field_names": [],
        "extra_field_names": [],
        "duplicate": [],
        "order_error": [],
    }


def _field_diagnostics(lines: Sequence[str]) -> Dict[str, Any]:
    """Project frame-shape errors without retaining arbitrary provider names."""

    diagnostics = _empty_field_diagnostics()
    observed: List[Optional[str]] = []
    counts: Dict[str, int] = {}
    for line in lines:
        if line.count("\t") != 1:
            observed.append(None)
            continue
        field = line.split("\t", 1)[0]
        if field in BUNDLE_FIELDS:
            observed.append(field)
            counts[field] = counts.get(field, 0) + 1
        else:
            # Do not echo an arbitrary provider-supplied field name.
            observed.append(None)
    diagnostics["missing_field_names"] = [field for field in BUNDLE_FIELDS if not counts.get(field)]
    diagnostics["duplicate"] = [field for field in BUNDLE_FIELDS if counts.get(field, 0) > 1]
    # Only fixed-schema names are safe to expose.  An unknown extra name is
    # represented by the expected public slot through order_error instead.
    diagnostics["extra_field_names"] = list(diagnostics["duplicate"])
    order_error: List[str] = []
    for index, field in enumerate(observed[: len(BUNDLE_FIELDS)]):
        expected = BUNDLE_FIELDS[index]
        if field != expected:
            order_error.append(expected)
    diagnostics["order_error"] = order_error
    return diagnostics


def _validation_categories(errors: Iterable[Any]) -> List[str]:
    """Map validator details to a fixed, body-free taxonomy."""

    categories = set()
    for raw in errors:
        error = str(raw)
        folded = error.casefold()
        if (
            "out_of_scope" in folded
            or "invalid_span" in folded
            or "cross_chat" in folded
            or "chat_mismatch" in folded
        ):
            categories.add("evidence_boundary")
        elif "evidence" in folded or "missing_evidence" in folded:
            categories.add("evidence_reference")
        elif "relation" in folded or "coreference" in folded or "conflict" in folded:
            categories.add("cross_chat_or_relation" if "relation" in folded else "conflict")
        elif any(token in folded for token in ("invalid_value", "invalid_type", "not_object", "not_list", "missing_fields")):
            categories.add("schema_enum_or_type")
        else:
            categories.add("other_validation")
    return sorted(category for category in categories if category in _VALIDATION_CATEGORIES)


class SemanticFrameParseError(ValueError):
    """A stable, body-free protocol or local-contract error."""

    def __init__(
        self,
        code: str,
        *,
        diagnostics: Optional[Mapping[str, Any]] = None,
        validation_categories: Optional[Iterable[Any]] = None,
    ) -> None:
        self.code = str(code)
        source = diagnostics if isinstance(diagnostics, Mapping) else _empty_field_diagnostics()
        self.diagnostics = {
            "missing_field_names": _public_field_names(source.get("missing_field_names")),
            "extra_field_names": _public_field_names(source.get("extra_field_names")),
            "duplicate": _public_field_names(source.get("duplicate")),
            "order_error": _public_field_names(source.get("order_error")),
        }
        self.validation_categories = tuple(
            sorted(
                category
                for category in (str(item) for item in (validation_categories or ()))
                if category in _VALIDATION_CATEGORIES
            )
        )
        super().__init__(self.code)


def _strict_json(value: str) -> Any:
    def reject_constant(_value: str) -> Any:
        raise SemanticFrameParseError("semantic_frame_nonstandard_json")

    try:
        return json.loads(value, parse_constant=reject_constant)
    except SemanticFrameParseError:
        raise
    except (TypeError, ValueError) as exc:
        raise SemanticFrameParseError("semantic_frame_invalid_json") from exc


def _frame_lines(text: Any, *, allow_prose: bool) -> List[str]:
    if not isinstance(text, str):
        raise SemanticFrameParseError("semantic_frame_response_not_text")
    lines = text.splitlines()
    if not lines:
        raise SemanticFrameParseError("semantic_frame_empty")
    output: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped in _FENCE_LINES:
            continue
        if allow_prose:
            output.append(stripped)
            continue
        if "\t" not in line:
            raise SemanticFrameParseError("semantic_frame_prose_forbidden")
        output.append(line)
    if not output:
        raise SemanticFrameParseError("semantic_frame_empty")
    return output


def parse_health_frame(text: Any) -> bool:
    """Extract one health frame, allowing only controlled wrapping prose."""

    lines = _frame_lines(text, allow_prose=True)
    frames = [line for line in lines if "\t" in line]
    if len(frames) != 1:
        raise SemanticFrameParseError("semantic_frame_health_frame_count")
    if frames[0].count("\t") != 1:
        raise SemanticFrameParseError("semantic_frame_health_field_shape")
    field, value = frames[0].split("\t")
    if field != "OK" or value != "true":
        raise SemanticFrameParseError("semantic_frame_health_invalid")
    # Any non-frame line is allowed only as explanation text.  A second
    # protocol-looking line is never silently ignored.
    for line in lines:
        if line == frames[0]:
            continue
        if "\t" in line:
            raise SemanticFrameParseError("semantic_frame_health_extra_field")
    return True


def _require_string(value: Any, code: str) -> None:
    if type(value) is not str:
        raise SemanticFrameParseError(code)


def _require_string_list(value: Any, code: str) -> None:
    if type(value) is not list or any(type(item) is not str for item in value):
        raise SemanticFrameParseError(code)


def _require_object(value: Any, code: str) -> None:
    if type(value) is not dict:
        raise SemanticFrameParseError(code)


def _require_object_list(value: Any, code: str) -> None:
    if type(value) is not list or any(type(item) is not dict for item in value):
        raise SemanticFrameParseError(code)


def _validate_field_type(field: str, value: Any) -> None:
    if field in {"schema_version", "bundle_id", "claim_type", "state", "modality"}:
        _require_string(value, "semantic_frame_%s_type" % field)
    elif field == "message_ids":
        _require_string_list(value, "semantic_frame_message_ids_type")
    elif field in {"speaker", "subject", "metadata"}:
        _require_object(value, "semantic_frame_%s_type" % field)
    elif field in {"mentioned_person", "target", "object", "action", "coreference_candidates", "context_relations", "uncertainties", "evidence"}:
        _require_object_list(value, "semantic_frame_%s_type" % field)
    else:  # pragma: no cover - guarded by the fixed field tuple
        raise SemanticFrameParseError("semantic_frame_extra_field")


def _validate_entity_shape(value: Any, field: str) -> None:
    if field not in {"speaker", "subject"}:
        return
    expected = {"id", "type", "role", "resolution", "evidence_ids"}
    if set(value) != expected:
        raise SemanticFrameParseError("semantic_frame_%s_shape" % field)
    for key in ("id", "type", "role", "resolution"):
        _require_string(value[key], "semantic_frame_%s_%s_type" % (field, key))
    _require_string_list(value["evidence_ids"], "semantic_frame_%s_evidence_ids_type" % field)


def parse_bundle_frame(
    text: Any,
    *,
    expected_schema_version: str = BUNDLE_SCHEMA_VERSION,
    expected_message_ids: Optional[Iterable[Any]] = None,
    expected_chat_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Parse one fixed-order bundle frame and run local evidence validation."""

    lines = _frame_lines(text, allow_prose=False)
    if len(lines) != len(BUNDLE_FIELDS):
        raise SemanticFrameParseError("semantic_frame_field_count", diagnostics=_field_diagnostics(lines))
    output: Dict[str, Any] = {}
    for index, line in enumerate(lines):
        if line.count("\t") != 1:
            raise SemanticFrameParseError("semantic_frame_field_shape", diagnostics=_field_diagnostics(lines))
        field, encoded = line.split("\t")
        expected = BUNDLE_FIELDS[index]
        if field != expected:
            if field in BUNDLE_FIELDS:
                raise SemanticFrameParseError("semantic_frame_field_order", diagnostics=_field_diagnostics(lines))
            raise SemanticFrameParseError("semantic_frame_extra_field", diagnostics=_field_diagnostics(lines))
        if field in output:
            raise SemanticFrameParseError("semantic_frame_duplicate_field", diagnostics=_field_diagnostics(lines))
        value = _strict_json(encoded)
        _validate_field_type(field, value)
        output[field] = value

    if output["schema_version"] != expected_schema_version:
        raise SemanticFrameParseError("semantic_frame_schema_version_mismatch")
    _validate_entity_shape(output["speaker"], "speaker")
    _validate_entity_shape(output["subject"], "subject")

    # Scalars are fixed enums at the frame boundary; unknown remains a valid
    # explicit abstention value.  Evidence linkage is checked below by the
    # canonical bundle validator, not inferred or repaired here.
    for field, allowed in (("claim_type", CLAIM_TYPES), ("state", STATES), ("modality", MODALITIES)):
        if output[field] not in allowed:
            raise SemanticFrameParseError("semantic_frame_%s_enum" % field)

    expected_ids: Optional[Tuple[str, ...]] = None
    if expected_message_ids is not None:
        expected_ids = tuple(str(item) for item in expected_message_ids)
        if tuple(output["message_ids"]) != expected_ids:
            raise SemanticFrameParseError("semantic_frame_message_ids_mismatch")
    report = validate_bundle(
        output,
        message_ids=expected_ids,
        chat_id=expected_chat_id,
        expected_schema_version=expected_schema_version,
    )
    if not report.ok:
        # Do not include validator details because a provider could have
        # supplied body-like values.  Only fixed taxonomy labels are retained.
        raise SemanticFrameParseError(
            "semantic_frame_bundle_validation_failed",
            validation_categories=_validation_categories(report.errors),
        )
    return output


__all__ = [
    "SEMANTIC_FRAME_VERSION",
    "SemanticFrameParseError",
    "parse_health_frame",
    "parse_bundle_frame",
]
