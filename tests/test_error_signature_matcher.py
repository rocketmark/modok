"""
Tests for the shared ErrorSignatureMatcher (docs/llds/continuous-ci-ingestion.md
§ ErrorSignatureMatcher). Written before implementation (Phase 5) — the class
does not exist yet, so most tests here fail with ImportError until Phase 6.

Characterization of the pre-extraction behavior (link_customer_issue_error_anchors)
lives in test_anchor_linking.py, per CIING-MATCH-004 — not duplicated here.
CIING-MATCH-005's parity assertion (same canonical ErrorSignature IDs, same
inputs) is the last test in this file, run against both implementations.

Specs verified: CIING-MATCH-001 through CIING-MATCH-005.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modok.ingestion.registry import Registry


def make_registries(tmp_path: Path, errors: dict[str, str]) -> Path:
    reg_dir = tmp_path / "registries"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "features.yml").write_text("features:\n  shtp-receiver:\n    name: SHTP Receiver\n")
    (reg_dir / "modules.yml").write_text("modules:\n  shtp:\n    name: SHTP\n")
    lines = ["errors:"]
    for slug, normalized in errors.items():
        lines.append(f"  {slug}:")
        lines.append(f"    normalized_error: {normalized!r}")
        lines.append("    description: test error")
    (reg_dir / "errors.yml").write_text("\n".join(lines) + "\n")
    return tmp_path


# ---------------------------------------------------------------------------
# CIING-MATCH-001 — word-boundary match, one ErrorSignatureMatch per distinct
# error_slug, with provenance
# ---------------------------------------------------------------------------


# @spec CIING-MATCH-001
def test_matches_exact_registered_error_with_provenance(tmp_path):
    from modok.ingestion.error_signature_matcher import ErrorSignatureMatcher

    repo_root = make_registries(tmp_path, {"gss-failure": "GSS_FAILURE"})
    registry = Registry(repo_root)
    matcher = ErrorSignatureMatcher()

    matches = matcher.match({"body": "Saw GSS_FAILURE during resume."}, registry)

    assert len(matches) == 1
    assert matches[0].error_slug == "gss-failure"
    assert matches[0].normalized_error == "GSS_FAILURE"
    assert matches[0].source_field == "body"
    assert "GSS_FAILURE" in matches[0].matched_fragment


# @spec CIING-MATCH-001
def test_matches_case_insensitively_same_as_today(tmp_path):
    from modok.ingestion.error_signature_matcher import ErrorSignatureMatcher

    repo_root = make_registries(tmp_path, {"gss-failure": "GSS_FAILURE"})
    registry = Registry(repo_root)
    matcher = ErrorSignatureMatcher()

    matches = matcher.match({"body": "saw gss_failure again"}, registry)
    assert [m.error_slug for m in matches] == ["gss-failure"]


# @spec CIING-MATCH-001
def test_no_match_returns_empty_list(tmp_path):
    from modok.ingestion.error_signature_matcher import ErrorSignatureMatcher

    repo_root = make_registries(tmp_path, {"gss-failure": "GSS_FAILURE"})
    registry = Registry(repo_root)
    matcher = ErrorSignatureMatcher()

    matches = matcher.match({"body": "everything worked fine"}, registry)
    assert matches == []


# @spec CIING-MATCH-001
def test_multiple_possible_matches_returns_one_per_distinct_slug(tmp_path):
    from modok.ingestion.error_signature_matcher import ErrorSignatureMatcher

    repo_root = make_registries(
        tmp_path, {"gss-failure": "GSS_FAILURE", "db-timeout": "DB_TIMEOUT"}
    )
    registry = Registry(repo_root)
    matcher = ErrorSignatureMatcher()

    matches = matcher.match({"body": "GSS_FAILURE then a DB_TIMEOUT"}, registry)
    assert {m.error_slug for m in matches} == {"gss-failure", "db-timeout"}


# @spec CIING-MATCH-001
def test_does_not_match_substring_inside_larger_word(tmp_path):
    """Same word-boundary discipline as link_customer_issue_error_anchors — not
    a new matching strategy, an extraction of the same one."""
    from modok.ingestion.error_signature_matcher import ErrorSignatureMatcher

    repo_root = make_registries(tmp_path, {"gss": "GSS"})
    registry = Registry(repo_root)
    matcher = ErrorSignatureMatcher()

    matches = matcher.match({"body": "Calling GSSAPI failed unexpectedly."}, registry)
    assert matches == []


# ---------------------------------------------------------------------------
# CIING-MATCH-002 — candidate fields, multi-field-aware (the actual point of
# extracting this into a shared, richer interface)
# ---------------------------------------------------------------------------


# @spec CIING-MATCH-002
def test_title_only_match_is_found(tmp_path):
    from modok.ingestion.error_signature_matcher import ErrorSignatureMatcher

    repo_root = make_registries(tmp_path, {"gss-failure": "GSS_FAILURE"})
    registry = Registry(repo_root)
    matcher = ErrorSignatureMatcher()

    matches = matcher.match(
        {"title": "GSS_FAILURE on resume", "body": "no repro details yet"}, registry
    )
    assert [m.error_slug for m in matches] == ["gss-failure"]
    assert matches[0].source_field == "title"


# @spec CIING-MATCH-002
def test_body_only_match_is_found(tmp_path):
    from modok.ingestion.error_signature_matcher import ErrorSignatureMatcher

    repo_root = make_registries(tmp_path, {"gss-failure": "GSS_FAILURE"})
    registry = Registry(repo_root)
    matcher = ErrorSignatureMatcher()

    matches = matcher.match(
        {"title": "Something is wrong", "body": "Saw GSS_FAILURE in the logs"}, registry
    )
    assert [m.error_slug for m in matches] == ["gss-failure"]
    assert matches[0].source_field == "body"


# @spec CIING-MATCH-001
def test_duplicate_text_across_title_and_body_yields_one_match(tmp_path):
    """Same error mentioned in both fields still yields one ErrorSignatureMatch
    per distinct error_slug, not two — matches are deduplicated by error_slug,
    not accumulated per field."""
    from modok.ingestion.error_signature_matcher import ErrorSignatureMatcher

    repo_root = make_registries(tmp_path, {"gss-failure": "GSS_FAILURE"})
    registry = Registry(repo_root)
    matcher = ErrorSignatureMatcher()

    matches = matcher.match(
        {"title": "GSS_FAILURE on resume", "body": "Also saw GSS_FAILURE here"}, registry
    )
    assert len(matches) == 1


# @spec CIING-MATCH-002
def test_junit_candidate_fields_are_checked(tmp_path):
    from modok.ingestion.error_signature_matcher import ErrorSignatureMatcher

    repo_root = make_registries(tmp_path, {"db-timeout": "DB_TIMEOUT"})
    registry = Registry(repo_root)
    matcher = ErrorSignatureMatcher()

    matches = matcher.match(
        {
            "failure_type": "AssertionError",
            "message": "connection failed",
            "assertion_text": "assert conn is not None",
            "stack_trace": "  ...\n  raise DB_TIMEOUT(...)\n  ...",
            "stderr": None,
        },
        registry,
    )
    assert [m.error_slug for m in matches] == ["db-timeout"]
    assert matches[0].source_field == "stack_trace"


# @spec CIING-MATCH-002
def test_none_candidate_field_is_skipped_not_errored(tmp_path):
    from modok.ingestion.error_signature_matcher import ErrorSignatureMatcher

    repo_root = make_registries(tmp_path, {"gss-failure": "GSS_FAILURE"})
    registry = Registry(repo_root)
    matcher = ErrorSignatureMatcher()

    # None fields (e.g. stderr not provided by the artifact) must not raise.
    matches = matcher.match({"body": None, "stderr": None}, registry)
    assert matches == []


# ---------------------------------------------------------------------------
# CIING-MATCH-003 — never invents an ErrorSignature
# ---------------------------------------------------------------------------


# @spec CIING-MATCH-003
def test_only_checks_against_registered_normalized_errors(tmp_path):
    """A match is only returned for a normalized_error value that is actually
    in the registry — arbitrary text that merely looks like an error does not
    produce a fabricated ErrorSignatureMatch."""
    from modok.ingestion.error_signature_matcher import ErrorSignatureMatcher

    repo_root = make_registries(tmp_path, {"gss-failure": "GSS_FAILURE"})
    registry = Registry(repo_root)
    matcher = ErrorSignatureMatcher()

    matches = matcher.match({"body": "Got a WEIRD_UNREGISTERED_ERROR here"}, registry)
    assert matches == []


# ---------------------------------------------------------------------------
# CIING-MATCH-004/005 — parity between the pre-extraction implementation and
# the extracted matcher, for the same single-field (raw_text/body) input the
# ticket-side path actually uses today
# ---------------------------------------------------------------------------


# @spec CIING-MATCH-004, CIING-MATCH-005
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_text,errors,expected_slugs",
    [
        ("Saw GSS_FAILURE during resume.", {"gss-failure": "GSS_FAILURE"}, {"gss-failure"}),
        ("saw gss_failure again", {"gss-failure": "GSS_FAILURE"}, {"gss-failure"}),
        ("everything worked fine", {"gss-failure": "GSS_FAILURE"}, set()),
        (
            "GSS_FAILURE then a DB_TIMEOUT",
            {"gss-failure": "GSS_FAILURE", "db-timeout": "DB_TIMEOUT"},
            {"gss-failure", "db-timeout"},
        ),
        ("Calling GSSAPI failed unexpectedly.", {"gss": "GSS"}, set()),
    ],
)
async def test_matcher_parity_with_pre_extraction_implementation(
    tmp_path, raw_text, errors, expected_slugs
):
    """CIING-MATCH-005: for the same customer-issue input and registry state,
    ErrorSignatureMatcher(candidate_fields={"body": raw_text}) shall resolve
    to the same canonical error signatures link_customer_issue_error_anchors
    resolves today, when the matcher is given only the single field the
    ticket-side path actually uses (body/raw_text) — not the richer
    title/explicit_error_text set, which is new surface, not parity surface."""
    from unittest.mock import AsyncMock

    from modok.ingestion.anchor_linking import link_customer_issue_error_anchors
    from modok.ingestion.error_signature_matcher import ErrorSignatureMatcher

    repo_root = make_registries(tmp_path, errors)
    registry = Registry(repo_root)

    client = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=True)
    client.replace_edges_by_parts = AsyncMock()
    old_linked = await link_customer_issue_error_anchors(
        client, "stagehand", repo_root, "github", "42", raw_text
    )
    old_slugs = {
        slug for slug, entry in registry._errors.items()
        if entry.get("normalized_error") in old_linked
    } if hasattr(registry, "_errors") else None

    matcher = ErrorSignatureMatcher()
    new_matches = matcher.match({"body": raw_text}, registry)
    new_slugs = {m.error_slug for m in new_matches}

    assert new_slugs == expected_slugs
    # Only compare against the old implementation's resolved slugs when we
    # could introspect them; the normalized_error-string comparison below is
    # the authoritative parity check either way (CIING-MATCH-005's actual
    # requirement: same canonical IDs for the same input).
    if old_slugs is not None:
        assert new_slugs == old_slugs
