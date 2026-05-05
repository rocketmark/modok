"""modok ingest-git command — import git commit history into the graph."""
# @spec SI-GIT-001, SI-GIT-005, SI-GIT-006, SI-GIT-010

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from modok.cli.config import ModokConfig
from modok.ingestion.discovery import discover_docs
from modok.ingestion.git_history import ingest_git
from modok.ingestion.registry import Registry
from modok.quine.client import QuineClient


@click.command("ingest-git")
@click.option("--project", required=True, help="Project slug.")
@click.option("--repo", default=None, help="Path to project repo (overrides config).")
@click.option("--full", is_flag=True, default=False,
              help="Import all history with no lookback limit.")
@click.option("--since", "since_date", default=None, metavar="DATE",
              help="Import commits authored after DATE (ISO-8601). Mutually exclusive with --full.")
@click.option("--max-commits", default=500, show_default=True,
              help="Maximum number of commits to import (default 500).")
def ingest_git_cmd(
    project: str,
    repo: str | None,
    full: bool,
    since_date: str | None,
    max_commits: int,
) -> None:
    """Import git commit history for registered files into the graph."""
    # SI-GIT-010: --full and --since are mutually exclusive
    if full and since_date:
        click.echo("Error: --full and --since are mutually exclusive", err=True)
        raise SystemExit(1)

    config = ModokConfig.load()
    proj = config.project(project)
    repo_root = Path(repo) if repo else Path(proj.repo)

    client = QuineClient(base_url=config.quine.url)
    if not asyncio.run(client.ping()):
        click.echo(
            f"Quine is not reachable at {config.quine.url} — "
            "run `modok quine start` or check your config",
            err=True,
        )
        raise SystemExit(2)

    registry = Registry(repo_root)

    try:
        registered_docs, _, _ = discover_docs(repo_root, registry)
        doc_paths = [
            str(d.path.relative_to(repo_root))
            for d in registered_docs
            if d.path.is_relative_to(repo_root)
        ]
    except Exception:
        doc_paths = []

    # Pass raw config dict for last_git_sha persistence
    config_dict = {"projects": [
        {"slug": p.slug, "repo": p.repo, "last_git_sha": getattr(p, "last_git_sha", None)}
        for p in config.projects
    ]}

    commit_count, edges_with_hunks, defs_found = asyncio.run(ingest_git(
        project_slug=project,
        repo_root=repo_root,
        registry=registry,
        client=client,
        config=config_dict,
        full=full,
        since_date=since_date,
        max_commits=max_commits,
        doc_paths=doc_paths,
    ))
    click.echo(
        f"Git history ingested for project '{project}': "
        f"{commit_count} commits written, "
        f"{edges_with_hunks} file-touches with hunk data, "
        f"{defs_found} function definitions found."
    )
