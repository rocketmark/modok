"""Metadata proposal verifier — pure function, no I/O, no LLM calls."""
# @spec LLM-VER-001, LLM-VER-002, LLM-VER-003, LLM-VER-004, LLM-VER-005,
#       LLM-VER-006, LLM-VER-007, LLM-VER-008, LLM-VER-009, LLM-VER-010,
#       LLM-VER-011

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from modok.llm.models import MetadataProposal
from modok.ingestion.registry import Registry

# Fields whose expected type is list[str]
_LIST_STR_FIELDS = {"modules", "source_files", "test_files", "error_signatures", "tags"}

# Known enum values per field
_ENUM_VALUES: dict[str, set[str]] = {
    "doc_type": {"hld", "lld", "spec", "testing", "runbook", "known-issue", "release-notes"},
}

# Slug fields and their registry lookup
_SLUG_CHECKS = {
    "feature_slug": ("has_feature", "feature_slugs"),
    "feature": ("has_feature", "feature_slugs"),
}
_MODULE_SLUG_LIST_FIELDS = {"modules"}
_ERROR_SLUG_LIST_FIELDS = {"error_signatures"}

# Evidence hard-check minimum length
_EVIDENCE_MIN_LEN = 15

# Known filler prefixes (case-insensitive startswith check)
_FILLER_PREFIXES = (
    "the document is about",
    "this document describes",
    "based on the document",
    "the file mentions",
)


@dataclass
class RejectedField:
    field: str
    bad_value: Any
    reason: str
    repair_instruction: str
    allowed_values: list[str] = field(default_factory=list)


@dataclass
class VerificationResult:
    valid_fields: dict[str, Any]
    rejected_fields: list[RejectedField]
    is_valid: bool


def _check_evidence(evidence: str) -> str | None:
    """Return a reason string if evidence fails, else None."""
    if not evidence or len(evidence) < _EVIDENCE_MIN_LEN:
        return "evidence insufficient — too short (minimum 15 characters)"
    lower = evidence.lower().strip()
    for prefix in _FILLER_PREFIXES:
        if lower.startswith(prefix):
            return f"evidence insufficient — matches known filler pattern: {prefix!r}"
    return None


def verify_proposal(
    proposal: MetadataProposal,
    missing_fields: list[str],
    existing_frontmatter: dict,
    registry: Registry,
) -> VerificationResult:
    """Pure verifier. Returns VerificationResult without mutating any input."""
    valid_fields: dict[str, Any] = {}
    rejected: list[RejectedField] = []

    # Evidence check is proposal-level — if it fails, all fields are rejected
    evidence_failure = _check_evidence(proposal.evidence)
    if evidence_failure:
        for key, val in proposal.proposed_fields.items():
            rejected.append(
                RejectedField(
                    field=key,
                    bad_value=val,
                    reason=evidence_failure,
                    repair_instruction="Provide a specific, non-generic evidence sentence (≥15 characters) quoting or paraphrasing the document body.",
                )
            )
        return VerificationResult(valid_fields={}, rejected_fields=rejected, is_valid=False)

    missing_set = set(missing_fields)

    for key, val in proposal.proposed_fields.items():
        # VER-001: field must be in missing_fields
        if key not in missing_set:
            rejected.append(
                RejectedField(
                    field=key,
                    bad_value=val,
                    reason="field was not requested in missing_fields",
                    repair_instruction="Only propose fields listed in missing_fields.",
                )
            )
            continue

        # VER-002: must not overwrite existing frontmatter
        if key in existing_frontmatter:
            rejected.append(
                RejectedField(
                    field=key,
                    bad_value=val,
                    reason=f"would overwrite existing frontmatter field '{key}'",
                    repair_instruction="Do not propose values for fields that already exist.",
                )
            )
            continue

        # VER-007: empty values not permitted
        if val is None or val == "" or val == []:
            rejected.append(
                RejectedField(
                    field=key,
                    bad_value=val,
                    reason="empty value not permitted",
                    repair_instruction="Provide a non-empty value or omit the field.",
                )
            )
            continue

        # VER-005: enum check
        if key in _ENUM_VALUES:
            allowed = _ENUM_VALUES[key]
            if val not in allowed:
                rejected.append(
                    RejectedField(
                        field=key,
                        bad_value=val,
                        reason=f"value {val!r} is not a known enum value for '{key}'",
                        repair_instruction="Use one of the allowed values.",
                        allowed_values=sorted(allowed),
                    )
                )
                continue

        # VER-004: single slug fields
        if key in _SLUG_CHECKS:
            has_method, slugs_method = _SLUG_CHECKS[key]
            if not getattr(registry, has_method)(val):
                allowed = getattr(registry, slugs_method, lambda: [])()
                rejected.append(
                    RejectedField(
                        field=key,
                        bad_value=val,
                        reason=f"slug {val!r} not found in registry",
                        repair_instruction="Choose a slug that exists in the feature registry, or omit the field.",
                        allowed_values=list(allowed),
                    )
                )
                continue

        # List field checks
        if key in _LIST_STR_FIELDS:
            # VER-003: must be a list
            if not isinstance(val, list):
                rejected.append(
                    RejectedField(
                        field=key,
                        bad_value=val,
                        reason=f"expected a list, got {type(val).__name__}",
                        repair_instruction="Provide a YAML list, not a scalar.",
                    )
                )
                continue

            # VER-006: no duplicates
            if len(val) != len(set(val)):
                rejected.append(
                    RejectedField(
                        field=key,
                        bad_value=val,
                        reason="list contains duplicates",
                        repair_instruction="Remove duplicate entries from the list.",
                    )
                )
                continue

            # VER-004: module slug validation
            if key in _MODULE_SLUG_LIST_FIELDS:
                bad_slugs = [s for s in val if not registry.has_module(s)]
                if bad_slugs:
                    rejected.append(
                        RejectedField(
                            field=key,
                            bad_value=val,
                            reason=f"unknown module slugs: {bad_slugs}",
                            repair_instruction="Use only slugs present in the module registry.",
                        )
                    )
                    continue

            # VER-004: error signature slug validation
            if key in _ERROR_SLUG_LIST_FIELDS:
                bad_slugs = [s for s in val if not registry.has_error(s)]
                if bad_slugs:
                    rejected.append(
                        RejectedField(
                            field=key,
                            bad_value=val,
                            reason=f"unknown error signature slugs: {bad_slugs}",
                            repair_instruction="Use only slugs present in the error registry.",
                        )
                    )
                    continue

        valid_fields[key] = val

    return VerificationResult(
        valid_fields=valid_fields,
        rejected_fields=rejected,
        is_valid=len(rejected) == 0,
    )
