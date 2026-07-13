"""
Tests for GitHub write-back: posting the Diagnostic Retrieval Engine's debug
packet as a comment on the originating GitHub issue when a standing query
fires. All tests are written before implementation (Phase 5). Every test
cites the EARS spec it verifies via @spec annotation.

Specs verified: SQ-GH-001, SQ-GH-002, SQ-GH-003, SQ-GH-004.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from modok.retrieval.formatting import format_debug_packet_markdown
from modok.retrieval.models import (
    AffectedArea,
    DebugPacket,
    IssueAnchors,
    IssueSummary,
    KnownIssueRef,
    PriorFix,
)


def make_packet(**overrides) -> DebugPacket:
    defaults = dict(
        issue=IssueSummary(
            summary="Client rejects v2 header",
            anchors=IssueAnchors(features=["shtp-receiver"], errors=["shtp-version-mismatch"], symptoms=[]),
        ),
        affected_areas=[AffectedArea(type="feature", id="feature:shtp-receiver", name="shtp-receiver")],
        relevant_files=["agent/src/shtp.c"],
        relevant_tests=[],
        known_issues=[KnownIssueRef(id="ki-shtp-version-mismatch", summary="Version mismatch corrupts calibration")],
        prior_fixes=[PriorFix(id="fix-shtp-version-offset", commit="a3f9c12", summary="Fix byte offset")],
        scored_candidates=[],
        summary="The client compares the version field at the wrong byte offset.",
    )
    defaults.update(overrides)
    return DebugPacket(**defaults)


# ---------------------------------------------------------------------------
# SQ-GH-002 — markdown formatting
# ---------------------------------------------------------------------------


# @spec SQ-GH-002
def test_markdown_includes_standing_query_name_and_investigation_id():
    md = format_debug_packet_markdown(make_packet(), "inv-42", "actionable-issue-pattern")
    assert "actionable-issue-pattern" in md
    assert "inv-42" in md


# @spec SQ-GH-002
def test_markdown_includes_summary_known_issues_and_fixes():
    md = format_debug_packet_markdown(make_packet(), "inv-42", "actionable-issue-pattern")
    assert "byte offset" in md
    assert "ki-shtp-version-mismatch" in md
    assert "fix-shtp-version-offset" in md
    assert "agent/src/shtp.c" in md


# @spec SQ-GH-002
def test_markdown_omits_empty_sections():
    packet = make_packet(relevant_tests=[], prior_fixes=[])
    md = format_debug_packet_markdown(packet, "inv-42", "actionable-issue-pattern")
    assert "Relevant tests" not in md
    assert "Prior fixes" not in md


# ---------------------------------------------------------------------------
# GitHub comment POST — best-effort, never raises
# ---------------------------------------------------------------------------


# @spec SQ-GH-001
@pytest.mark.asyncio
async def test_post_issue_comment_sends_body_to_correct_url():
    requests_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(201, json={"id": 1})

    from modok.ingestion.github import post_issue_comment

    with patch("httpx.AsyncClient") as mock_cls:
        mock_instance = mock_cls.return_value.__aenter__.return_value
        mock_instance.post = AsyncMock(return_value=httpx.Response(201, json={"id": 1}))
        await post_issue_comment("acme/stagehand", "tok", "42", "hello world")
        call = mock_instance.post.call_args
        assert "acme/stagehand" in call.args[0] or "acme/stagehand" in str(call)
        assert "42" in str(call)


# @spec SQ-GH-004
@pytest.mark.asyncio
async def test_post_issue_comment_failure_is_swallowed():
    from modok.ingestion.github import post_issue_comment

    with patch("httpx.AsyncClient") as mock_cls:
        mock_instance = mock_cls.return_value.__aenter__.return_value
        mock_instance.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        await post_issue_comment("acme/stagehand", "tok", "42", "hello world")  # must not raise


# ---------------------------------------------------------------------------
# SQ-GH-001 / SQ-GH-003 — the run_ingest_event write-back gate
# ---------------------------------------------------------------------------


# @spec SQ-GH-001
@pytest.mark.asyncio
async def test_maybe_notify_github_posts_when_configured():
    from modok.webhook.server import _maybe_notify_github

    fake_project = type("P", (), {"slug": "stagehand", "github_repo": "acme/stagehand"})()
    fake_config = type("C", (), {"projects": [fake_project]})()

    with patch("modok.cli.config.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}), \
         patch("modok.retrieval.engine.retrieve", new=AsyncMock(return_value=make_packet())), \
         patch("modok.ingestion.github.post_issue_comment", new=AsyncMock()) as mock_post:
        await _maybe_notify_github(
            client=AsyncMock(),
            project_slug="stagehand",
            source_system="github",
            ticket_id="42",
            investigation_id="inv-42",
            standing_query_name="actionable-issue-pattern",
        )

    mock_post.assert_called_once()


# @spec SQ-GH-003
@pytest.mark.asyncio
async def test_maybe_notify_github_skips_when_token_missing():
    from modok.webhook.server import _maybe_notify_github

    fake_project = type("P", (), {"slug": "stagehand", "github_repo": "acme/stagehand"})()
    fake_config = type("C", (), {"projects": [fake_project]})()

    with patch("modok.cli.config.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {}, clear=True), \
         patch("modok.ingestion.github.post_issue_comment", new=AsyncMock()) as mock_post:
        await _maybe_notify_github(
            client=AsyncMock(),
            project_slug="stagehand",
            source_system="github",
            ticket_id="42",
            investigation_id="inv-42",
            standing_query_name="actionable-issue-pattern",
        )  # must not raise

    mock_post.assert_not_called()


# @spec SQ-GH-003
@pytest.mark.asyncio
async def test_maybe_notify_github_skips_when_repo_not_configured():
    from modok.webhook.server import _maybe_notify_github

    fake_project = type("P", (), {"slug": "stagehand", "github_repo": None})()
    fake_config = type("C", (), {"projects": [fake_project]})()

    with patch("modok.cli.config.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}), \
         patch("modok.ingestion.github.post_issue_comment", new=AsyncMock()) as mock_post:
        await _maybe_notify_github(
            client=AsyncMock(),
            project_slug="stagehand",
            source_system="github",
            ticket_id="42",
            investigation_id="inv-42",
            standing_query_name="actionable-issue-pattern",
        )

    mock_post.assert_not_called()


# @spec SQ-GH-003
@pytest.mark.asyncio
async def test_maybe_notify_github_skips_for_non_github_source():
    from modok.webhook.server import _maybe_notify_github

    with patch("modok.ingestion.github.post_issue_comment", new=AsyncMock()) as mock_post:
        await _maybe_notify_github(
            client=AsyncMock(),
            project_slug="stagehand",
            source_system="webhook",
            ticket_id="42",
            investigation_id="inv-42",
            standing_query_name="actionable-issue-pattern",
        )

    mock_post.assert_not_called()


# @spec SQ-GH-004
@pytest.mark.asyncio
async def test_maybe_notify_github_swallows_dre_failure():
    from modok.webhook.server import _maybe_notify_github

    fake_project = type("P", (), {"slug": "stagehand", "github_repo": "acme/stagehand"})()
    fake_config = type("C", (), {"projects": [fake_project]})()

    with patch("modok.cli.config.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}), \
         patch("modok.retrieval.engine.retrieve", new=AsyncMock(side_effect=Exception("DRE unavailable"))), \
         patch("modok.ingestion.github.post_issue_comment", new=AsyncMock()) as mock_post:
        await _maybe_notify_github(
            client=AsyncMock(),
            project_slug="stagehand",
            source_system="github",
            ticket_id="42",
            investigation_id="inv-42",
            standing_query_name="actionable-issue-pattern",
        )  # must not raise

    mock_post.assert_not_called()
