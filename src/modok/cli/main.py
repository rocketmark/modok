"""Top-level CLI entry point for MODOK."""
# @spec CLI-STAT-001, CLI-STAT-002, CLI-STAT-003, CLI-STAT-004, CLI-STAT-005

from __future__ import annotations

import asyncio
import sys

import click

from modok.cli.commands.extract_code_map import extract_code_map_cmd
from modok.cli.commands.import_arrow import import_arrow_cmd
from modok.cli.commands.init import init_cmd
from modok.cli.commands.normalise import normalise_cmd
from modok.cli.commands.ingest import ingest_cmd
from modok.cli.commands.ingest_git import ingest_git_cmd
from modok.cli.commands.ingest_github import ingest_github_cmd
from modok.cli.commands.ingest_elements import ingest_elements_cmd
from modok.cli.commands.retrieve import retrieve_cmd
from modok.cli.commands.recall import recall_cmd
from modok.cli.commands.search import search_cmd
from modok.cli.commands.list import list_cmd
from modok.cli.commands.diagnose import diagnose_cmd
from modok.cli.commands.quine import quine_cmd
from modok.cli.commands.serve import serve_command
from modok.cli.commands.stream import stream_cmd
from modok.cli.config import ModokConfig
from modok.quine.client import QuineClient


def _run_status() -> None:
    config = ModokConfig.load()
    client = QuineClient(base_url=config.quine.url)
    reachable = asyncio.run(client.ping())

    if reachable:
        click.echo(f"Quine:    running at {config.quine.url}")
        try:
            count_rows = asyncio.run(client.query("MATCH (n) RETURN count(n) AS total", {}))
            total = count_rows[0][0] if count_rows and count_rows[0] else 0

            type_rows = asyncio.run(
                client.query(
                    "MATCH (n) RETURN DISTINCT n.node_type, count(n) ORDER BY n.node_type", {}
                )
            )
            click.echo(f"Nodes:    {total} total")
            for row in type_rows:
                if not row or len(row) < 2:
                    continue
                node_type = row[0] if row[0] is not None else "(untyped)"
                count = row[1]
                click.echo(f"  {node_type:<16}{count}")
        except Exception as exc:
            click.echo(f"  (could not query nodes: {exc})", err=True)
    else:
        click.echo(f"Quine:    not reachable at {config.quine.url}")

    click.echo("")
    click.echo("Projects:")
    for proj in config.projects:
        click.echo(f"  {proj.slug:<16}{proj.repo}")


@click.group(invoke_without_command=True)
@click.version_option(version="0.1.0", prog_name="modok")
@click.option("--status", is_flag=True, default=False, help="Show Quine status and graph summary.")
@click.pass_context
def cli(ctx: click.Context, status: bool) -> None:
    """MODOK — diagnostic memory graph CLI."""
    if status:
        _run_status()
        sys.exit(0)
    elif ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


cli.add_command(extract_code_map_cmd, name="extract-code-map")
cli.add_command(import_arrow_cmd, name="import-arrow")
cli.add_command(init_cmd, name="init")
cli.add_command(normalise_cmd, name="normalise")
cli.add_command(ingest_cmd, name="ingest")
cli.add_command(ingest_git_cmd, name="ingest-git")
cli.add_command(ingest_github_cmd, name="ingest-github")
cli.add_command(ingest_elements_cmd, name="ingest-elements")
cli.add_command(retrieve_cmd, name="retrieve")
cli.add_command(recall_cmd, name="recall")
cli.add_command(search_cmd, name="search")
cli.add_command(list_cmd, name="list")
cli.add_command(diagnose_cmd, name="diagnose")
cli.add_command(quine_cmd, name="quine")
cli.add_command(serve_command, name="serve")
cli.add_command(stream_cmd, name="stream")
