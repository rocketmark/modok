"""
Tests for modok.webhook.adapters.github_poll.GitHubPollAdapter — polls
GitHub for new issues/PRs on an interval so a live demo needs no public
webhook tunnel. All tests are written before implementation (Phase 5).
Every test cites the EARS spec it verifies via @spec annotation.

Specs verified: SQ-POLL-001, SQ-POLL-002, SQ-POLL-003, SQ-POLL-004, SQ-POLL-005, SQ-POLL-006.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, patch

import pytest

from modok.webhook.adapters.github_poll import GitHubPollAdapter
from modok.webhook.models import WebhookConfig


def _project(slug="stagehand", github_repo="acme/stagehand"):
    return type("P", (), {"slug": slug, "github_repo": github_repo, "last_github_sync": None})()


def _config_with_projects(projects):
    fake_quine = type("Q", (), {"url": "http://127.0.0.1:8080"})()
    return type("C", (), {"projects": projects, "quine": fake_quine})()


# ---------------------------------------------------------------------------
# SQ-POLL-001 — implements PullAdapter protocol unchanged
# ---------------------------------------------------------------------------


# @spec SQ-POLL-001
def test_implements_pull_adapter_protocol():
    adapter = GitHubPollAdapter()
    assert inspect.iscoroutinefunction(adapter.start)
    assert inspect.iscoroutinefunction(adapter.stop)
    sig = inspect.signature(adapter.start)
    assert list(sig.parameters) == ["config", "on_event"]


# ---------------------------------------------------------------------------
# SQ-POLL-002 — polls opted-in projects on the configured interval
# ---------------------------------------------------------------------------


# @spec SQ-POLL-002
@pytest.mark.asyncio
async def test_polls_project_with_github_repo_when_enabled():
    config = WebhookConfig(github_poll_enabled=True, github_poll_interval_seconds=0.01)
    fake_config = _config_with_projects([_project()])

    mock_ingester = AsyncMock()
    mock_ingester.run = AsyncMock(
        return_value=type("R", (), {"issues_written": 0, "prs_written": 0})()
    )

    adapter = GitHubPollAdapter()
    with patch("modok.webhook.adapters.github_poll.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}), \
         patch("modok.webhook.adapters.github_poll.GithubIngester", return_value=mock_ingester), \
         patch("modok.webhook.adapters.github_poll.save_last_github_sync") as mock_save:
        await adapter.start(config, AsyncMock())
        await asyncio.sleep(0.05)
        await adapter.stop()

    assert mock_ingester.run.await_count >= 1
    assert mock_save.call_count >= 1


# ---------------------------------------------------------------------------
# SQ-POLL-003 — disabled means no polling at all
# ---------------------------------------------------------------------------


# @spec SQ-POLL-003
@pytest.mark.asyncio
async def test_no_polling_when_disabled():
    config = WebhookConfig(github_poll_enabled=False, github_poll_interval_seconds=0.01)
    fake_config = _config_with_projects([_project()])

    mock_ingester_cls = AsyncMock()

    adapter = GitHubPollAdapter()
    with patch("modok.webhook.adapters.github_poll.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}), \
         patch("modok.webhook.adapters.github_poll.GithubIngester", mock_ingester_cls):
        await adapter.start(config, AsyncMock())
        await asyncio.sleep(0.05)
        await adapter.stop()

    mock_ingester_cls.assert_not_called()


# ---------------------------------------------------------------------------
# SQ-POLL-004 — projects without github_repo or GITHUB_TOKEN are skipped
# ---------------------------------------------------------------------------


# @spec SQ-POLL-004
@pytest.mark.asyncio
async def test_skips_project_without_github_repo():
    config = WebhookConfig(github_poll_enabled=True, github_poll_interval_seconds=0.01)
    fake_config = _config_with_projects([_project(github_repo=None)])

    mock_ingester_cls = AsyncMock()

    adapter = GitHubPollAdapter()
    with patch("modok.webhook.adapters.github_poll.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}), \
         patch("modok.webhook.adapters.github_poll.GithubIngester", mock_ingester_cls):
        await adapter.start(config, AsyncMock())
        await asyncio.sleep(0.05)
        await adapter.stop()

    mock_ingester_cls.assert_not_called()


# @spec SQ-POLL-004
@pytest.mark.asyncio
async def test_skips_project_without_github_token_env_var():
    config = WebhookConfig(github_poll_enabled=True, github_poll_interval_seconds=0.01)
    fake_config = _config_with_projects([_project()])

    mock_ingester_cls = AsyncMock()

    adapter = GitHubPollAdapter()
    with patch("modok.webhook.adapters.github_poll.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {}, clear=True), \
         patch("modok.webhook.adapters.github_poll.GithubIngester", mock_ingester_cls):
        await adapter.start(config, AsyncMock())
        await asyncio.sleep(0.05)
        await adapter.stop()  # must not raise

    mock_ingester_cls.assert_not_called()


# @spec SQ-POLL-004
@pytest.mark.asyncio
async def test_no_output_when_project_has_no_github_repo(capsys):
    config = WebhookConfig(github_poll_enabled=True, github_poll_interval_seconds=0.01)
    fake_config = _config_with_projects([_project(github_repo=None)])

    adapter = GitHubPollAdapter()
    with patch("modok.webhook.adapters.github_poll.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}):
        await adapter.start(config, AsyncMock())
        await asyncio.sleep(0.05)
        await adapter.stop()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


# @spec SQ-POLL-004
@pytest.mark.asyncio
async def test_logs_warning_when_github_repo_set_but_token_missing(capsys):
    config = WebhookConfig(github_poll_enabled=True, github_poll_interval_seconds=0.01)
    fake_config = _config_with_projects([_project(slug="stagehand")])

    adapter = GitHubPollAdapter()
    with patch("modok.webhook.adapters.github_poll.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {}, clear=True):
        await adapter.start(config, AsyncMock())
        await asyncio.sleep(0.05)
        await adapter.stop()

    captured = capsys.readouterr()
    assert "stagehand" in captured.err
    assert "GITHUB_TOKEN" in captured.err


# ---------------------------------------------------------------------------
# SQ-POLL-006 — successful sync prints a one-line summary
# ---------------------------------------------------------------------------


# @spec SQ-POLL-006
@pytest.mark.asyncio
async def test_logs_success_summary_after_sync(capsys):
    config = WebhookConfig(github_poll_enabled=True, github_poll_interval_seconds=0.01)
    fake_config = _config_with_projects([_project(slug="stagehand")])

    mock_ingester = AsyncMock()
    mock_ingester.run = AsyncMock(
        return_value=type("R", (), {"issues_written": 2, "prs_written": 1})()
    )

    adapter = GitHubPollAdapter()
    with patch("modok.webhook.adapters.github_poll.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}), \
         patch("modok.webhook.adapters.github_poll.GithubIngester", return_value=mock_ingester), \
         patch("modok.webhook.adapters.github_poll.save_last_github_sync"):
        await adapter.start(config, AsyncMock())
        await asyncio.sleep(0.05)
        await adapter.stop()

    captured = capsys.readouterr()
    assert "stagehand" in captured.out
    assert "2" in captured.out
    assert "1" in captured.out


# ---------------------------------------------------------------------------
# SQ-POLL-005 — stop() cancels cleanly
# ---------------------------------------------------------------------------


# @spec SQ-POLL-005
@pytest.mark.asyncio
async def test_stop_cancels_background_task_without_raising():
    config = WebhookConfig(github_poll_enabled=True, github_poll_interval_seconds=0.01)
    fake_config = _config_with_projects([_project()])

    mock_ingester = AsyncMock()
    mock_ingester.run = AsyncMock(
        return_value=type("R", (), {"issues_written": 0, "prs_written": 0})()
    )

    adapter = GitHubPollAdapter()
    with patch("modok.webhook.adapters.github_poll.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}), \
         patch("modok.webhook.adapters.github_poll.GithubIngester", return_value=mock_ingester), \
         patch("modok.webhook.adapters.github_poll.save_last_github_sync"):
        await adapter.start(config, AsyncMock())
        await adapter.stop()  # must not raise
        await adapter.stop()  # calling stop twice must also not raise


# @spec SQ-POLL-005
@pytest.mark.asyncio
async def test_stop_without_start_does_not_raise():
    adapter = GitHubPollAdapter()
    await adapter.stop()
