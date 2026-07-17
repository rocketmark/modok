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

from modok.cli.config import CONFIG_PATH, ModokConfig, ProjectConfig
from modok.ingestion.ci_ingestion import (
    discover_workflow_runs,
    expand_workflow_run,
    find_expansion_backlog,
    reconcile_commit_edges,
    reconcile_file_escalations,
    reconcile_root_cause_escalation_status,
    reconcile_root_cause_escalations,
    reconcile_test_execution_links,
    save_last_workflow_sync,
)
from modok.ingestion.dependency_ingestion import (
    reconcile_dependency_change_edges,
    run_dependency_ingestion_cycle,
)
from modok.ingestion.github import GithubIngester, reconcile_deleted_tickets, save_last_github_sync
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

            # @spec GHING-DEL-009, GHING-DEL-010
            try:
                await reconcile_deleted_tickets(quine_client, project.slug, project.github_repo, token)
            except Exception as exc:
                print(
                    f"github-poll: {project.slug} — deleted-ticket reconciliation failed: {exc}",
                    file=sys.stderr,
                )

            ci_summary = await _run_ci_ingestion_cycle(quine_client, project, token)
            dependency_summary = await _run_dependency_ingestion_cycle(quine_client, project, token)

            # @spec SQ-POLL-006
            print(
                f"github-poll: {project.slug} — synced {report.issues_written} issue(s), "
                f"{report.prs_written} PR(s){ci_summary}{dependency_summary}"
            )


# @spec CIING-POLL-001, CIING-POLL-002, CIING-POLL-003, CIING-POLL-005, CIING-EDGE-005
async def _run_ci_ingestion_cycle(quine_client: QuineClient, project: ProjectConfig, token: str) -> str:
    """Continuous CI ingestion, extending the existing per-project poll cycle
    (§ Poll Cycle Extension). Each of the three steps is isolated from the
    others and from the issue/PR sync above — a failure discovering workflow
    runs, expanding one run, or reconciling commit edges must not prevent the
    other steps, other projects, or later cycles from proceeding. Returns a
    trailing summary fragment for the existing one-line log (empty when
    nothing happened, so a project with no CI activity doesn't add noise).
    """
    workflow_sync_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    discovered = 0
    expanded = 0

    try:
        runs = await discover_workflow_runs(
            quine_client,
            project.slug,
            project.github_repo,
            token,
            since=project.last_workflow_sync or None,
        )
        discovered = len(runs)
        # @spec CIING-POLL-001 — advance the discovery cursor immediately after
        # discovery, independent of whether any run below finishes expanding.
        save_last_workflow_sync(CONFIG_PATH, project.slug, workflow_sync_start)
    except Exception as exc:
        print(
            f"github-poll: {project.slug} — workflow run discovery failed: {exc}",
            file=sys.stderr,
        )

    try:
        backlog = await find_expansion_backlog(quine_client, project.slug)
    except Exception as exc:
        print(
            f"github-poll: {project.slug} — expansion backlog query failed: {exc}",
            file=sys.stderr,
        )
        backlog = []

    for run_id in backlog:
        # @spec CIING-POLL-005 — one run's expansion failure must not block the rest
        try:
            await expand_workflow_run(
                quine_client,
                project.slug,
                run_id=run_id,
                token=token,
                github_repo=project.github_repo,
                artifact_pattern=project.ci_artifact_pattern,
            )
            expanded += 1
        except Exception as exc:
            print(
                f"github-poll: {project.slug} — expansion failed for run {run_id}: {exc}",
                file=sys.stderr,
            )

    try:
        await reconcile_commit_edges(quine_client, project.slug)
    except Exception as exc:
        print(
            f"github-poll: {project.slug} — commit edge reconciliation failed: {exc}",
            file=sys.stderr,
        )

    # @spec TCLINK-POLL-005
    try:
        await reconcile_test_execution_links(quine_client, project.slug)
    except Exception as exc:
        print(
            f"github-poll: {project.slug} — test-execution link reconciliation failed: {exc}",
            file=sys.stderr,
        )

    # @spec FESC-POLL-003
    try:
        await reconcile_file_escalations(quine_client, project.slug)
    except Exception as exc:
        print(
            f"github-poll: {project.slug} — file-escalation reconciliation failed: {exc}",
            file=sys.stderr,
        )

    # @spec RCESC-POLL-003
    try:
        await reconcile_root_cause_escalations(quine_client, project.slug)
    except Exception as exc:
        print(
            f"github-poll: {project.slug} — root-cause-escalation reconciliation failed: {exc}",
            file=sys.stderr,
        )

    # @spec RCESC-STATUS-007
    try:
        await reconcile_root_cause_escalation_status(
            quine_client, project.slug, project.github_repo, token
        )
    except Exception as exc:
        print(
            f"github-poll: {project.slug} — root-cause-escalation status sync failed: {exc}",
            file=sys.stderr,
        )

    if not discovered and not expanded:
        return ""
    return f", {discovered} workflow run(s) checked, {expanded} expanded"


# @spec DEPG-POLL-001, DEPG-POLL-006
async def _run_dependency_ingestion_cycle(
    quine_client: QuineClient, project: ProjectConfig, token: str
) -> str:
    """Dependency-graph ingestion, on its own cursor, isolated from issue/PR
    sync and CI ingestion above — a failure here must not prevent either of
    those, or a later cycle, from proceeding (docs/llds/dependency-graph-
    ingestion.md § Polling and Checkpoint Behavior)."""
    try:
        summary = await run_dependency_ingestion_cycle(
            quine_client,
            project.slug,
            project.github_repo,
            token,
            since=project.last_dependency_sync or None,
            config_path=CONFIG_PATH,
        )
    except Exception as exc:
        print(
            f"github-poll: {project.slug} — dependency ingestion failed: {exc}",
            file=sys.stderr,
        )
        return ""

    try:
        await reconcile_dependency_change_edges(quine_client, project.slug)
    except Exception as exc:
        print(
            f"github-poll: {project.slug} — dependency edge reconciliation failed: {exc}",
            file=sys.stderr,
        )

    return summary
