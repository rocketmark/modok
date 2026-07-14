"""GitHubPollAdapter — polls GitHub for new issues/PRs on an interval so a
live demo needs no public webhook tunnel. Implements the existing
PullAdapter protocol unchanged. See docs/llds/standing-queries.md § GitHub
Poll Adapter."""
# @spec SQ-POLL-001, SQ-POLL-002, SQ-POLL-003, SQ-POLL-004, SQ-POLL-005, SQ-POLL-006

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from modok.cli.config import CONFIG_PATH, ModokConfig
from modok.ingestion.github import GithubIngester, save_last_github_sync
from modok.quine.client import QuineClient
from modok.webhook.models import IngestEvent, WebhookConfig


class GitHubPollAdapter:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    # @spec SQ-POLL-001, SQ-POLL-003
    async def start(
        self, config: WebhookConfig, on_event: Callable[[IngestEvent], Awaitable[None]]
    ) -> None:
        if not config.github_poll_enabled:
            return
        self._task = asyncio.create_task(self._poll_loop(config))

    # @spec SQ-POLL-005
    async def stop(self) -> None:
        if self._task is None:
            return
        task = self._task
        self._task = None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _poll_loop(self, config: WebhookConfig) -> None:
        while True:
            await self._poll_once()
            await asyncio.sleep(config.github_poll_interval_seconds)

    # @spec SQ-POLL-002, SQ-POLL-004
    async def _poll_once(self) -> None:
        try:
            modok_config = ModokConfig.load()
        except Exception as exc:
            print(f"github-poll: could not load config: {exc}", file=sys.stderr)
            return

        token = os.environ.get("GITHUB_TOKEN")

        for project in modok_config.projects:
            # @spec SQ-POLL-004 — no github_repo: silent, expected common case
            if not project.github_repo:
                continue
            # @spec SQ-POLL-004 — github_repo set but no token: likely misconfiguration, log it
            if not token:
                print(
                    f"github-poll: {project.slug} — skipped (GITHUB_TOKEN not set)",
                    file=sys.stderr,
                )
                continue

            quine_client = QuineClient(base_url=modok_config.quine.url)
            ingester = GithubIngester(
                project_slug=project.slug,
                github_repo=project.github_repo,
                token=token,
                client=quine_client,
            )
            sync_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            since = project.last_github_sync or None

            try:
                report = await ingester.run(since=since)
            except Exception as exc:
                print(f"github-poll: sync failed for {project.slug}: {exc}", file=sys.stderr)
                continue

            save_last_github_sync(CONFIG_PATH, project.slug, sync_start)
            # @spec SQ-POLL-006
            print(
                f"github-poll: {project.slug} — synced {report.issues_written} issue(s), "
                f"{report.prs_written} PR(s)"
            )
