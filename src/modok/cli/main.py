"""Top-level CLI entry point for MODOK."""

from __future__ import annotations

import click

from modok.cli.commands.init import init_cmd
from modok.cli.commands.ingest import ingest_cmd
from modok.cli.commands.retrieve import retrieve_cmd
from modok.cli.commands.recall import recall_cmd
from modok.cli.commands.quine import quine_cmd


@click.group()
@click.version_option(version="0.1.0", prog_name="modok")
def cli() -> None:
    """MODOK — diagnostic memory graph CLI."""


cli.add_command(init_cmd, name="init")
cli.add_command(ingest_cmd, name="ingest")
cli.add_command(retrieve_cmd, name="retrieve")
cli.add_command(recall_cmd, name="recall")
cli.add_command(quine_cmd, name="quine")
