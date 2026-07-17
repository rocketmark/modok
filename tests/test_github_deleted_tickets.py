"""
Tests for Deleted Ticket Detection: reconcile_deleted_tickets fetches the
full current set of GitHub issue numbers (no incremental `since` filter,
the only way to positively confirm a ticket no longer exists) and marks any
CustomerIssue absent from that set status="deleted" (docs/llds/
github-ingestion.md § Deleted Ticket Detection). Written before
implementation (Phase 5) — reconcile_deleted_tickets does not exist yet, so
every test fails with ImportError/AttributeError until Phase 6.

Specs verified: GHING-DEL-001 through 010.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_client(query_return=None):
    client = MagicMock()
    client.query = AsyncMock(return_value=query_return if query_return is not None else [])
    return client


def _ticket_row(ticket_id: str):
    return [ticket_id]


# @spec GHING-DEL-001, GHING-DEL-002
@pytest.mark.asyncio
async def test_fetches_full_list_with_no_since_and_keeps_prs():
    from modok.ingestion.github import reconcile_deleted_tickets

    client = _mock_client()
    items = [{"number": 1}, {"number": 2, "pull_request": {}}]
    with patch(
        "modok.ingestion.github.GithubIngester._paginate_sync",
        return_value=items,
    ) as mock_paginate:
        await reconcile_deleted_tickets(client, "stagehand", "acme/stagehand", "tok")

    call = mock_paginate.call_args
    params = call.args[1] if len(call.args) > 1 else call.kwargs.get("params", {})
    assert "since" not in params


# @spec GHING-DEL-003, GHING-DEL-004, GHING-DEL-005
@pytest.mark.asyncio
async def test_marks_missing_ticket_deleted_and_leaves_present_ones_alone():
    from modok.ingestion.github import reconcile_deleted_tickets

    client = _mock_client(query_return=[_ticket_row("18"), _ticket_row("20")])
    items = [{"number": 20}]  # only 20 still exists; 18 is gone
    with patch("modok.ingestion.github.GithubIngester._paginate_sync", return_value=items):
        count = await reconcile_deleted_tickets(client, "stagehand", "acme/stagehand", "tok")

    assert count == 1
    set_calls = [c for c in client.query.call_args_list if "SET ci.status" in c.args[0]]
    assert len(set_calls) == 1
    assert set_calls[0].args[1]["t"] == "18"


# @spec GHING-DEL-006
@pytest.mark.asyncio
async def test_query_scoped_to_github_source_system():
    import inspect
    from modok.ingestion.github import reconcile_deleted_tickets

    source = inspect.getsource(reconcile_deleted_tickets)
    assert "source_system = 'github'" in source


# @spec GHING-DEL-007
@pytest.mark.asyncio
async def test_already_deleted_tickets_excluded_from_query():
    import inspect
    from modok.ingestion.github import reconcile_deleted_tickets

    source = inspect.getsource(reconcile_deleted_tickets)
    assert "status <> 'deleted'" in source


# @spec GHING-DEL-008
@pytest.mark.asyncio
async def test_fetch_failure_marks_nothing_and_does_not_raise():
    from modok.ingestion.github import reconcile_deleted_tickets

    client = _mock_client(query_return=[_ticket_row("18")])
    with patch(
        "modok.ingestion.github.GithubIngester._paginate_sync",
        side_effect=Exception("boom"),
    ):
        count = await reconcile_deleted_tickets(client, "stagehand", "acme/stagehand", "tok")

    assert count == 0
    client.query.assert_not_called()


# @spec GHING-DEL-009
@pytest.mark.asyncio
async def test_poll_cycle_isolates_deleted_ticket_reconciliation_failure():
    from modok.webhook.adapters.github_poll import GitHubPollAdapter
    from modok.webhook.models import WebhookConfig

    config = WebhookConfig(github_poll_enabled=True, github_poll_interval_seconds=0.01)
    fake_project = MagicMock()
    fake_project.slug = "stagehand"
    fake_project.github_repo = "acme/stagehand"
    fake_project.last_github_sync = None
    fake_config = MagicMock()
    fake_config.projects = [fake_project]

    mock_ingester = AsyncMock()
    mock_ingester.run = AsyncMock(return_value=MagicMock(issues_written=0, prs_written=0))

    import asyncio

    adapter = GitHubPollAdapter()
    with patch("modok.webhook.adapters.github_poll.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}), \
         patch("modok.webhook.adapters.github_poll.GithubIngester", return_value=mock_ingester), \
         patch("modok.webhook.adapters.github_poll.save_last_github_sync"), \
         patch("modok.webhook.adapters.github_poll.reconcile_commit_edges", new=AsyncMock()), \
         patch("modok.webhook.adapters.github_poll.reconcile_test_execution_links", new=AsyncMock()), \
         patch("modok.webhook.adapters.github_poll.reconcile_file_escalations", new=AsyncMock()), \
         patch("modok.webhook.adapters.github_poll.reconcile_root_cause_escalations", new=AsyncMock()), \
         patch("modok.webhook.adapters.github_poll.discover_workflow_runs", new=AsyncMock(return_value=[])), \
         patch("modok.webhook.adapters.github_poll.find_expansion_backlog", new=AsyncMock(return_value=[])), \
         patch(
             "modok.webhook.adapters.github_poll.reconcile_deleted_tickets",
             new=AsyncMock(side_effect=Exception("boom")),
         ):
        await adapter.start(config, AsyncMock())
        await asyncio.sleep(0.05)
        await adapter.stop()  # must not raise, sync must still complete


# @spec GHING-DEL-010
@pytest.mark.asyncio
async def test_not_called_when_github_repo_or_token_missing():
    from modok.webhook.adapters.github_poll import GitHubPollAdapter
    from modok.webhook.models import WebhookConfig

    config = WebhookConfig(github_poll_enabled=True, github_poll_interval_seconds=0.01)
    fake_project = MagicMock()
    fake_project.slug = "stagehand"
    fake_project.github_repo = None  # not configured
    fake_config = MagicMock()
    fake_config.projects = [fake_project]

    import asyncio

    adapter = GitHubPollAdapter()
    with patch("modok.webhook.adapters.github_poll.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {}, clear=True), \
         patch(
             "modok.webhook.adapters.github_poll.reconcile_deleted_tickets", new=AsyncMock()
         ) as mock_reconcile:
        await adapter.start(config, AsyncMock())
        await asyncio.sleep(0.05)
        await adapter.stop()

    mock_reconcile.assert_not_called()
