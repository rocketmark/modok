"""modok ingest command."""
# @spec CLI-INGEST-001, CLI-INGEST-002, CLI-INGEST-003, CLI-INGEST-004, CLI-INGEST-005, CLI-PING-001

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click

from modok.cli.config import ModokConfig
from modok.ingestion.pipeline import run_ingestion
from modok.ingestion.registry import Registry
from modok.quine.client import QuineClient


@click.command("ingest")
@click.option("--project", required=True, help="Project slug.")
@click.option("--fix", is_flag=True, default=False, help="Enable LLM proposal pass.")
@click.argument("path", type=click.Path())
def ingest_cmd(project: str, fix: bool, path: str) -> None:
    config = ModokConfig.load()
    proj = config.project(project)

    client = QuineClient(base_url=config.quine.url)
    if not _sync_ping(client):
        url = config.quine.url
        click.echo(
            f"Quine is not reachable at {url} — run `modok quine start` or check your config",
            err=True,
        )
        raise SystemExit(2)

    fix_mode = fix
    if fix and not sys.stdin.isatty():
        fix_mode = False
        click.echo(
            "Warning: LLM fix proposals suppressed in non-interactive mode.",
            err=True,
        )

    repo_root = Path(proj.repo)
    registry = Registry(repo_root)
    report = asyncio.run(run_ingestion(
        repo_root=repo_root,
        registry=registry,
        client=client,
        project_slug=project,
        fix_mode=fix_mode,
    ))
    click.echo(str(report))
    if report.errors:
        raise SystemExit(3)


def _sync_ping(client: QuineClient) -> bool:
    return asyncio.run(client.ping())
