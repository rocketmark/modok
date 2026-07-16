"""
Tests for the GitHub event routing unification (docs/llds/continuous-ci-ingestion.md
§ Prerequisite: Unified GitHub Event Routing). Written before implementation (Phase 5).

The existing poll/batch-path characterization already lives in
test_ingestion_github.py (test_ingest_merged_pr, test_implemented_in_edge_written,
test_resolved_by_edge_written, test_ingest_open_dependabot_pr_becomes_customer_issue,
etc.) — those tests assert on mock_client.upsert_node/write_edge_by_parts call
shapes, which is exactly what must stay identical once ingest_issue/ingest_pr
dispatch through run_ingest_event instead of mutating inline. They are the
GHING-ROUTE-006/007 parity baseline; nothing here duplicates them.

This file covers: (a) today's webhook-path thinness as an explicit baseline
(must pass now), (b) the new FixData fields and run_ingest_event fix-branch
behavior that don't exist yet (must fail now, until Phase 6), (c) the
asyncio.to_thread dispatch requirement (GHING-ROUTE-008).

Specs verified: GHING-ROUTE-001 through GHING-ROUTE-008.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modok.quine.models import CustomerIssue, Fix
from modok.webhook.models import FixData, IngestEvent


# ---------------------------------------------------------------------------
# Baseline characterization — today's webhook path is thinner than poll/batch.
# These must PASS against current code; they prove the gap GHING-ROUTE-003
# closes actually exists, so its "closing a gap" framing isn't just asserted.
# ---------------------------------------------------------------------------


def _make_merged_pr_payload(number: int = 42, merge_commit_sha: str = "abc123", body: str = "") -> dict:
    return {
        "action": "closed",
        "merged": True,
        "pull_request": {
            "number": number,
            "title": "Fix the widget",
            "merge_commit_sha": merge_commit_sha,
            "html_url": f"https://github.com/owner/repo/pull/{number}",
            "body": body,
            "user": {"login": "alice"},
        },
    }


def test_baseline_run_ingest_event_fix_branch_writes_no_pr_edges_today():
    """Today's fix branch only upserts a bare Fix node — no IMPLEMENTED_IN,
    no RESOLVED_BY, no dependabot handling. Characterizes the pre-unification
    behavior GHING-ROUTE-004 extends."""
    from modok.webhook.server import run_ingest_event

    mock_client = MagicMock()
    mock_client.upsert_node = AsyncMock()
    mock_client.write_edge_by_parts = AsyncMock()

    event = IngestEvent(
        kind="fix",
        project_slug="stagehand",
        data=FixData(fix_id="gh-42", summary="Fix the widget", kind="pull-request"),
    )
    run_ingest_event(event, mock_client)

    mock_client.upsert_node.assert_called_once()
    node = mock_client.upsert_node.call_args[0][0]
    assert isinstance(node, Fix)
    mock_client.write_edge_by_parts.assert_not_called()


# ---------------------------------------------------------------------------
# GHING-ROUTE-003 — FixData gains PR-specific fields
# ---------------------------------------------------------------------------


# @spec GHING-ROUTE-003
def test_fixdata_gains_pr_specific_fields():
    data = FixData(
        fix_id="gh-42",
        summary="Fix the widget",
        pr_url="https://github.com/owner/repo/pull/42",
        merge_commit_sha="abc123",
        closing_issue_numbers=["7"],
        is_open_dependabot=False,
    )
    assert data.pr_url == "https://github.com/owner/repo/pull/42"
    assert data.merge_commit_sha == "abc123"
    assert data.closing_issue_numbers == ["7"]
    assert data.is_open_dependabot is False


# @spec GHING-ROUTE-003
def test_webhook_merged_pr_populates_pr_specific_fields():
    from modok.webhook.adapters.github import GitHubAdapter

    adapter = GitHubAdapter()
    payload = _make_merged_pr_payload(number=42, merge_commit_sha="deadbeef", body="closes #7")
    event = adapter.normalize_event(payload, "pull_request")

    assert event.data.merge_commit_sha == "deadbeef"
    assert event.data.closing_issue_numbers == ["7"]
    assert event.data.is_open_dependabot is False
    assert event.data.pr_url == "https://github.com/owner/repo/pull/42"


# ---------------------------------------------------------------------------
# GHING-ROUTE-004 — run_ingest_event's fix branch performs the full PR handling
# ---------------------------------------------------------------------------


def _mock_client(node_exists: bool = True) -> MagicMock:
    client = MagicMock()
    client.upsert_node = AsyncMock()
    client.write_edge_by_parts = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=node_exists)
    return client


# @spec GHING-ROUTE-004
def test_fix_branch_open_dependabot_upserts_customer_issue_not_fix():
    from modok.webhook.server import run_ingest_event

    mock_client = _mock_client()
    event = IngestEvent(
        kind="fix",
        project_slug="stagehand",
        data=FixData(
            fix_id="gh-77", summary="Bump lodash", is_open_dependabot=True,
            pr_url=None, merge_commit_sha=None, closing_issue_numbers=[],
        ),
    )
    run_ingest_event(event, mock_client)

    mock_client.upsert_node.assert_called_once()
    node = mock_client.upsert_node.call_args[0][0]
    assert isinstance(node, CustomerIssue)
    assert node.ticket_id == "77"
    mock_client.write_edge_by_parts.assert_not_called()


# @spec GHING-ROUTE-004
def test_fix_branch_writes_implemented_in_when_commit_exists():
    from modok.webhook.server import run_ingest_event

    mock_client = _mock_client(node_exists=True)
    event = IngestEvent(
        kind="fix",
        project_slug="stagehand",
        data=FixData(
            fix_id="gh-42", summary="Fix widget", merge_commit_sha="deadbeef",
            closing_issue_numbers=[], is_open_dependabot=False, pr_url=None,
        ),
    )
    run_ingest_event(event, mock_client)

    calls = [str(c) for c in mock_client.write_edge_by_parts.call_args_list]
    assert any("IMPLEMENTED_IN" in c for c in calls)


# @spec GHING-ROUTE-004
def test_fix_branch_skips_implemented_in_when_commit_absent():
    from modok.webhook.server import run_ingest_event

    mock_client = _mock_client(node_exists=False)
    event = IngestEvent(
        kind="fix",
        project_slug="stagehand",
        data=FixData(
            fix_id="gh-42", summary="Fix widget", merge_commit_sha="deadbeef",
            closing_issue_numbers=[], is_open_dependabot=False, pr_url=None,
        ),
    )
    run_ingest_event(event, mock_client)

    calls = [str(c) for c in mock_client.write_edge_by_parts.call_args_list]
    assert not any("IMPLEMENTED_IN" in c for c in calls)


# @spec GHING-ROUTE-004
def test_fix_branch_writes_resolved_by_when_issue_exists():
    from modok.webhook.server import run_ingest_event

    mock_client = _mock_client(node_exists=True)
    event = IngestEvent(
        kind="fix",
        project_slug="stagehand",
        data=FixData(
            fix_id="gh-42", summary="Fix widget", closing_issue_numbers=["7"],
            merge_commit_sha=None, is_open_dependabot=False, pr_url=None,
        ),
    )
    run_ingest_event(event, mock_client)

    calls = [str(c) for c in mock_client.write_edge_by_parts.call_args_list]
    assert any("RESOLVED_BY" in c for c in calls)


# ---------------------------------------------------------------------------
# GHING-ROUTE-001/002/008 — ingest_issue/ingest_pr dispatch through
# run_ingest_event via asyncio.to_thread, not inline mutation
# ---------------------------------------------------------------------------


@pytest.fixture()
def poll_mock_client():
    client = MagicMock()
    client.upsert_node = AsyncMock()
    client.write_edge_by_parts = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=True)
    return client


@pytest.fixture()
def poll_ingester(poll_mock_client):
    from modok.ingestion.github import GithubIngester

    return GithubIngester(
        project_slug="stagehand", github_repo="owner/repo", token="fake-token",
        client=poll_mock_client,
    )


# @spec GHING-ROUTE-001, GHING-ROUTE-008
@pytest.mark.asyncio
async def test_ingest_issue_dispatches_via_run_ingest_event(poll_ingester, poll_mock_client):
    with patch("modok.ingestion.github.run_ingest_event") as mock_run:
        await poll_ingester.ingest_issue(
            {"number": 1, "title": "T", "body": "B", "state": "open", "labels": []}
        )
    mock_run.assert_called_once()
    event = mock_run.call_args[0][0]
    assert isinstance(event, IngestEvent)
    assert event.kind == "customer_issue"
    assert event.data.ticket_id == "1"


# @spec GHING-ROUTE-002, GHING-ROUTE-008
@pytest.mark.asyncio
async def test_ingest_pr_dispatches_via_run_ingest_event(poll_ingester, poll_mock_client):
    pr = {
        "number": 42, "title": "Fix widget", "body": "",
        "merged_at": "2024-01-15T10:00:00Z", "merge_commit_sha": "abc123",
        "user": {"login": "alice"}, "html_url": "https://github.com/owner/repo/pull/42",
    }
    with patch("modok.ingestion.github.run_ingest_event") as mock_run:
        await poll_ingester.ingest_pr(pr)
    mock_run.assert_called_once()
    event = mock_run.call_args[0][0]
    assert isinstance(event, IngestEvent)
    assert event.kind == "fix"
    assert event.data.fix_id == "gh-42"


# @spec GHING-ROUTE-008
@pytest.mark.asyncio
async def test_ingest_issue_dispatch_uses_to_thread_not_direct_call():
    """run_ingest_event is synchronous and calls asyncio.run() internally —
    calling it directly from within ingest_issue's already-running event loop
    would raise. Confirm the dispatch goes through asyncio.to_thread."""
    from modok.ingestion.github import GithubIngester

    mock_client = MagicMock()
    ingester = GithubIngester(
        project_slug="stagehand", github_repo="owner/repo", token="tok", client=mock_client
    )
    with patch("modok.ingestion.github.asyncio.to_thread", new=AsyncMock()) as mock_to_thread:
        await ingester.ingest_issue(
            {"number": 1, "title": "T", "body": "B", "state": "open", "labels": []}
        )
    mock_to_thread.assert_awaited_once()
    # First positional arg to asyncio.to_thread is the callable being dispatched.
    dispatched_fn = mock_to_thread.call_args[0][0]
    assert dispatched_fn.__name__ == "run_ingest_event"


# ---------------------------------------------------------------------------
# GHING-ROUTE-005 — duplicated anchor-linking implementation removed
# ---------------------------------------------------------------------------


# @spec GHING-ROUTE-005
def test_github_ingester_no_longer_has_its_own_link_anchors():
    from modok.ingestion.github import GithubIngester

    assert not hasattr(GithubIngester, "_link_anchors"), (
        "GithubIngester._link_anchors should be removed once ingest_issue "
        "dispatches through run_ingest_event, which already performs "
        "anchor linking — see GHING-ROUTE-005."
    )
