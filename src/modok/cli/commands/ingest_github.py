"""modok ingest-github command — pull GitHub issues and PRs into the graph."""
# @spec GHING-CONF-001, GHING-CONF-002, GHING-CONF-003, GHING-AUTH-002,
#       GHING-SYNC-002, GHING-SYNC-003, GHING-ERR-001, GHING-ERR-002

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import click

from modok.cli.config import CONFIG_PATH, ModokConfig
from modok.ingestion.github import GithubIngester, save_last_github_sync
from modok.quine.client import QuineClient


@click.command("ingest-github")
@click.option("--project", required=True, help="Project slug.")
@click.option("--full", is_flag=True, default=False,
              help="Fetch all issues and PRs, ignoring last_github_sync.")
def ingest_github_cmd(project: str, full: bool) -> None:
    """Pull GitHub issues and merged PRs into the graph."""
    config = ModokConfig.load()
    proj = config.project(project)

    # @spec GHING-CONF-001
    if not proj.github_repo:
        raise click.ClickException(
            f"github_repo not set for project `{project}` — "
            f"add `github_repo = \"owner/repo\"` to config"
        )

    # @spec GHING-CONF-002
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise click.ClickException("GITHUB_TOKEN environment variable not set")

    # @spec GHING-ERR-001
    client = QuineClient(base_url=config.quine.url)
    if not asyncio.run(client.ping()):
        click.echo(
            f"Quine is not reachable at {config.quine.url} — "
            "run `modok quine start` or check your config",
            err=True,
        )
        raise SystemExit(2)

    # Record sync start time before any API calls (GHING-CONF-003)
    sync_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # @spec GHING-SYNC-001, GHING-SYNC-002, GHING-SYNC-003
    since = None if full or not proj.last_github_sync else proj.last_github_sync

    ingester = GithubIngester(
        project_slug=project,
        github_repo=proj.github_repo,
        token=token,
        client=client,
    )

    try:
        report = asyncio.run(ingester.run(since=since))
    except SystemExit:
        raise
    except Exception as exc:
        click.echo(f"GitHub ingestion failed: {exc}", err=True)
        raise SystemExit(2)

    # @spec GHING-CONF-003
    save_last_github_sync(CONFIG_PATH, project, sync_start)

    click.echo(str(report))
