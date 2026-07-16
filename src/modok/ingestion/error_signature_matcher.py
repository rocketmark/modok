"""ErrorSignatureMatcher — a shared, deterministic, registry-backed matcher
used by both ticket-side (CustomerIssue) and JUnit-side (TestFailure) error
matching. Extraction, not rewrite: the matching algorithm (word-boundary,
case-insensitive substring against registry.error_normalized_values()) is the
same one link_customer_issue_error_anchors already used — see
docs/llds/continuous-ci-ingestion.md § ErrorSignatureMatcher."""
# @spec CIING-MATCH-001, CIING-MATCH-002, CIING-MATCH-003

from __future__ import annotations

import re
from dataclasses import dataclass

from modok.ingestion.registry import Registry

# Priority order when the same error appears in multiple candidate fields —
# the first field (in this order) containing a match is reported as
# source_field (CIING-MATCH-002). Covers both ticket fields (title, body,
# explicit_error_text) and JUnit fields (failure_type, message,
# assertion_text, stack_trace, stderr).
_CANDIDATE_FIELD_ORDER = (
    "title",
    "body",
    "explicit_error_text",
    "failure_type",
    "message",
    "assertion_text",
    "stack_trace",
    "stderr",
)


@dataclass(frozen=True)
class ErrorSignatureMatch:
    error_slug: str
    normalized_error: str
    source_field: str
    matched_fragment: str


class ErrorSignatureMatcher:
    # @spec CIING-MATCH-001, CIING-MATCH-002, CIING-MATCH-003
    def match(self, fields: dict[str, str | None], registry: Registry) -> list[ErrorSignatureMatch]:
        """One ErrorSignatureMatch per distinct registered error_slug that
        appears (word-boundary, case-insensitive) in any candidate field.
        Never invents a match for text that merely looks like an error —
        only checks against registry.error_normalized_values() (CIING-MATCH-003)."""
        matches: list[ErrorSignatureMatch] = []
        for slug, entry in registry._errors.items():
            if not isinstance(entry, dict):
                continue
            normalized_error = entry.get("normalized_error")
            if not normalized_error:
                continue
            pattern = re.compile(rf"\b{re.escape(normalized_error)}\b", re.IGNORECASE)
            for field_name in _CANDIDATE_FIELD_ORDER:
                text = fields.get(field_name)
                if not text:
                    continue
                found = pattern.search(text)
                if found:
                    matches.append(
                        ErrorSignatureMatch(
                            error_slug=slug,
                            normalized_error=normalized_error,
                            source_field=field_name,
                            matched_fragment=found.group(0),
                        )
                    )
                    break
        return matches
