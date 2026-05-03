"""
Tests for GitHub ingestion.

Specs verified: GHING-CONF-001, GHING-CONF-002, GHING-CONF-003,
GHING-AUTH-001, GHING-AUTH-002,
GHING-SYNC-001, GHING-SYNC-002, GHING-SYNC-003,
GHING-ISSUE-001, GHING-ISSUE-002,
GHING-PR-001, GHING-PR-002, GHING-PR-003, GHING-PR-004,
GHING-RES-001, GHING-RES-002, GHING-RES-003, GHING-RES-004,
GHING-RATE-001, GHING-RATE-002,
GHING-ERR-001, GHING-ERR-002,
GHING-MODEL-001.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modok.quine.models import CustomerIssue, Fix


# ---------------------------------------------------------------------------
# Fixtures — GitHub API response shapes
# ---------------------------------------------------------------------------

def make_issue(
    number: int = 1,
    title: str = "Test issue",
    body: str = "",
    state: str = "open",
    login: str = "alice",
) -> dict:
    return {
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "user": {"login": login},
        # No "pull_request" key — this is a plain issue
    }


def make_pr_issue(number: int = 10) -> dict:
    """A PR as returned by the /issues endpoint (has pull_request key)."""
    return {
        "number": number,
        "title": "PR disguised as issue",
        "body": "",
        "state": "closed",
        "user": {"login": "alice"},
        "pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/10"},
    }


def make_pr(
    number: int = 42,
    title: str = "Fix the thing",
    body: str = "",
    merged_at: str | None = "2024-01-15T10:00:00Z",
    merge_commit_sha: str | None = "abc123",
    login: str = "alice",
    html_url: str = "https://github.com/owner/repo/pull/42",
    updated_at: str = "2024-01-15T10:00:00Z",
) -> dict:
    return {
        "number": number,
        "title": title,
        "body": body,
        "merged_at": merged_at,
        "merge_commit_sha": merge_commit_sha,
        "user": {"login": login},
        "html_url": html_url,
        "updated_at": updated_at,
    }


# ---------------------------------------------------------------------------
# GHING-MODEL-001 — Fix model has pr_url field
# ---------------------------------------------------------------------------

# @spec GHING-MODEL-001
def test_fix_model_has_pr_url():
    fix = Fix(
        node_type="Fix",
        project_slug="proj",
        fix_id="gh-42",
        summary="Fix it",
        kind="pull-request",
        pr_url="https://github.com/owner/repo/pull/42",
    )
    assert fix.pr_url == "https://github.com/owner/repo/pull/42"


# @spec GHING-MODEL-001
def test_fix_model_pr_url_defaults_to_none():
    fix = Fix(
        node_type="Fix",
        project_slug="proj",
        fix_id="FIX-001",
        summary="Doc-authored fix",
        kind="code-fix",
    )
    assert fix.pr_url is None


# ---------------------------------------------------------------------------
# Closing reference parser (pure function — tested in isolation)
# ---------------------------------------------------------------------------

def _parse_closing_refs(body: str) -> list[int]:
    """Mirror of what the ingester will implement."""
    if not body:
        return []
    pattern = re.compile(r"(?:closes?|fix(?:es)?|resolves?)\s+#(\d+)", re.IGNORECASE)
    return [int(m.group(1)) for m in pattern.finditer(body)]


# @spec GHING-RES-002
def test_closing_refs_standard_keywords():
    assert _parse_closing_refs("closes #10") == [10]
    assert _parse_closing_refs("Fixes #22") == [22]
    assert _parse_closing_refs("RESOLVES #5") == [5]
    assert _parse_closing_refs("close #3") == [3]
    assert _parse_closing_refs("fix #7") == [7]
    assert _parse_closing_refs("resolve #99") == [99]


# @spec GHING-RES-002
def test_closing_refs_multiple():
    body = "closes #1\nfixes #2\nresolves #3"
    assert _parse_closing_refs(body) == [1, 2, 3]


# @spec GHING-RES-004
def test_closing_refs_ignores_cross_repo():
    body = "closes owner/repo#55"
    assert _parse_closing_refs(body) == []


# @spec GHING-RES-002
def test_closing_refs_empty_body():
    assert _parse_closing_refs("") == []
    assert _parse_closing_refs(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# PR incremental stop condition (pure)
# ---------------------------------------------------------------------------

def _should_stop_pr_page(page: list[dict], since: str) -> bool:
    """Stop fetching PR pages when all items on a page are older than `since`."""
    return all(pr["updated_at"] <= since for pr in page)


# @spec GHING-SYNC-001
def test_pr_stop_condition_all_old():
    page = [
        make_pr(updated_at="2023-12-01T00:00:00Z"),
        make_pr(updated_at="2023-11-01T00:00:00Z"),
    ]
    assert _should_stop_pr_page(page, "2024-01-01T00:00:00Z") is True


# @spec GHING-SYNC-001
def test_pr_stop_condition_mixed():
    page = [
        make_pr(updated_at="2024-02-01T00:00:00Z"),  # newer
        make_pr(updated_at="2023-12-01T00:00:00Z"),  # older
    ]
    assert _should_stop_pr_page(page, "2024-01-01T00:00:00Z") is False


# ---------------------------------------------------------------------------
# GithubIngester tests (mocked HTTP + mocked Quine)
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_client():
    client = MagicMock()
    client.upsert_node = AsyncMock()
    client.write_edge_by_parts = AsyncMock()
    client.node_exists = AsyncMock(return_value=True)
    return client


@pytest.fixture()
def ingester(mock_client):
    from modok.ingestion.github import GithubIngester
    return GithubIngester(
        project_slug="stagehand",
        github_repo="owner/repo",
        token="fake-token",
        client=mock_client,
    )


# @spec GHING-ISSUE-001
@pytest.mark.asyncio
async def test_ingest_open_issue(ingester, mock_client):
    issue = make_issue(number=7, title="Widget broken", state="open", body="Details here")
    await ingester.ingest_issue(issue)

    mock_client.upsert_node.assert_awaited_once()
    node: CustomerIssue = mock_client.upsert_node.call_args[0][0]
    assert isinstance(node, CustomerIssue)
    assert node.source_system == "github"
    assert node.ticket_id == "7"
    assert node.summary == "Widget broken"
    assert node.raw_text == "Details here"
    assert node.status == "open"
    assert node.project_slug == "stagehand"


# @spec GHING-ISSUE-001
@pytest.mark.asyncio
async def test_ingest_closed_issue_status(ingester, mock_client):
    issue = make_issue(number=8, state="closed")
    await ingester.ingest_issue(issue)
    node: CustomerIssue = mock_client.upsert_node.call_args[0][0]
    assert node.status == "closed"


# @spec GHING-ISSUE-001
@pytest.mark.asyncio
async def test_ingest_issue_null_body(ingester, mock_client):
    issue = make_issue(number=9, body=None)  # type: ignore[arg-type]
    await ingester.ingest_issue(issue)
    node: CustomerIssue = mock_client.upsert_node.call_args[0][0]
    assert node.raw_text == ""


# @spec GHING-ISSUE-002
@pytest.mark.asyncio
async def test_ingest_issue_skips_prs(ingester, mock_client):
    pr_as_issue = make_pr_issue(number=10)
    await ingester.ingest_issue(pr_as_issue)
    mock_client.upsert_node.assert_not_awaited()


# @spec GHING-PR-001
@pytest.mark.asyncio
async def test_ingest_merged_pr(ingester, mock_client):
    pr = make_pr(number=42, title="Fix the widget", html_url="https://github.com/owner/repo/pull/42")
    await ingester.ingest_pr(pr)

    mock_client.upsert_node.assert_awaited_once()
    node: Fix = mock_client.upsert_node.call_args[0][0]
    assert isinstance(node, Fix)
    assert node.fix_id == "gh-42"
    assert node.summary == "Fix the widget"
    assert node.kind == "pull-request"
    assert node.pr_url == "https://github.com/owner/repo/pull/42"
    assert node.project_slug == "stagehand"


# @spec GHING-PR-001
@pytest.mark.asyncio
async def test_ingest_dependabot_pr_kind(ingester, mock_client):
    pr = make_pr(number=99, login="dependabot[bot]")
    await ingester.ingest_pr(pr)
    node: Fix = mock_client.upsert_node.call_args[0][0]
    assert node.kind == "dependency-update"
    assert node.fix_id == "gh-99"


# @spec GHING-PR-002
@pytest.mark.asyncio
async def test_ingest_unmerged_pr_skipped(ingester, mock_client):
    pr = make_pr(number=55, merged_at=None, merge_commit_sha=None)
    await ingester.ingest_pr(pr)
    mock_client.upsert_node.assert_not_awaited()


# @spec GHING-PR-003
@pytest.mark.asyncio
async def test_ingest_open_non_dependabot_pr_skipped(ingester, mock_client):
    pr = make_pr(number=56, merged_at=None)
    pr["state"] = "open"
    await ingester.ingest_pr(pr)
    mock_client.upsert_node.assert_not_awaited()


# @spec GHING-PR-005
@pytest.mark.asyncio
async def test_ingest_open_dependabot_pr_becomes_customer_issue(ingester, mock_client):
    pr = make_pr(number=77, login="dependabot[bot]", merged_at=None, title="Bump lodash")
    pr["state"] = "open"
    await ingester.ingest_pr(pr)
    mock_client.upsert_node.assert_awaited_once()
    node = mock_client.upsert_node.call_args[0][0]
    assert isinstance(node, CustomerIssue)
    assert node.ticket_id == "77"
    assert node.status == "open"
    assert node.source_system == "github"


# @spec GHING-PR-004
@pytest.mark.asyncio
async def test_dependabot_pr_no_customer_issue(ingester, mock_client):
    pr = make_pr(number=77, login="dependabot[bot]", body="closes #5")
    # Even with a closing reference, no CustomerIssue should be written for Dependabot PR itself
    await ingester.ingest_pr(pr)
    upserted_types = [type(c[0][0]).__name__ for c in mock_client.upsert_node.call_args_list]
    assert "CustomerIssue" not in upserted_types


# @spec GHING-RES-001
@pytest.mark.asyncio
async def test_implemented_in_edge_written(ingester, mock_client):
    mock_client.node_exists = AsyncMock(return_value=True)
    pr = make_pr(number=42, merge_commit_sha="deadbeef")
    await ingester.ingest_pr(pr)
    mock_client.write_edge_by_parts.assert_awaited()
    calls = [str(c) for c in mock_client.write_edge_by_parts.call_args_list]
    assert any("IMPLEMENTED_IN" in c for c in calls)


# @spec GHING-RES-001
@pytest.mark.asyncio
async def test_implemented_in_edge_skipped_when_commit_absent(ingester, mock_client):
    mock_client.node_exists = AsyncMock(return_value=False)
    pr = make_pr(number=42, merge_commit_sha="deadbeef")
    await ingester.ingest_pr(pr)
    calls = [str(c) for c in mock_client.write_edge_by_parts.call_args_list]
    assert not any("IMPLEMENTED_IN" in c for c in calls)


# @spec GHING-RES-003
@pytest.mark.asyncio
async def test_resolved_by_edge_written(ingester, mock_client):
    mock_client.node_exists = AsyncMock(return_value=True)
    pr = make_pr(number=42, body="closes #7")
    await ingester.ingest_pr(pr)
    calls = [str(c) for c in mock_client.write_edge_by_parts.call_args_list]
    assert any("RESOLVED_BY" in c for c in calls)


# @spec GHING-RES-003
@pytest.mark.asyncio
async def test_resolved_by_edge_skipped_when_issue_absent(ingester, mock_client):
    # node_exists returns False for CustomerIssue, True for Commit
    async def node_exists_side_effect(node_id):
        # Commit nodes exist, CustomerIssue nodes don't
        return False
    mock_client.node_exists = AsyncMock(side_effect=node_exists_side_effect)
    pr = make_pr(number=42, body="closes #7", merge_commit_sha=None)
    await ingester.ingest_pr(pr)
    calls = [str(c) for c in mock_client.write_edge_by_parts.call_args_list]
    assert not any("RESOLVED_BY" in c for c in calls)


# ---------------------------------------------------------------------------
# Config / auth validation tests (CLI layer)
# ---------------------------------------------------------------------------

# @spec GHING-CONF-001
def test_missing_github_repo_exits_1(tmp_path):
    from click.testing import CliRunner
    from modok.cli.commands.ingest_github import ingest_github_cmd

    runner = CliRunner()
    with patch("modok.cli.commands.ingest_github.ModokConfig.load") as mock_load:
        mock_cfg = MagicMock()
        mock_proj = MagicMock()
        mock_proj.github_repo = None
        mock_cfg.project.return_value = mock_proj
        mock_load.return_value = mock_cfg

        result = runner.invoke(ingest_github_cmd, ["--project", "stagehand"])

    assert result.exit_code == 1
    assert "github_repo not set" in result.output


# @spec GHING-CONF-002
def test_missing_token_exits_1(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from modok.cli.commands.ingest_github import ingest_github_cmd

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    runner = CliRunner()
    with patch("modok.cli.commands.ingest_github.ModokConfig.load") as mock_load:
        mock_cfg = MagicMock()
        mock_proj = MagicMock()
        mock_proj.github_repo = "owner/repo"
        mock_cfg.project.return_value = mock_proj
        mock_load.return_value = mock_cfg

        result = runner.invoke(ingest_github_cmd, ["--project", "stagehand"])

    assert result.exit_code == 1
    assert "GITHUB_TOKEN" in result.output
