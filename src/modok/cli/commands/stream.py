"""modok stream subcommands: install | status | remove.

Standing queries are Quine-instance-level infrastructure, not per-project
data (project isolation is already provided by idFrom() node-address
topology — see docs/llds/standing-queries.md § Standing Query Definition),
so unlike most other commands these take no --project option."""
# @spec SQ-CLI-001, SQ-CLI-002, SQ-CLI-003, SQ-CLI-004, SQ-CLI-005

from __future__ import annotations

import asyncio

import click

from modok.cli.config import ModokConfig
from modok.quine.client import QuineClient
from modok.quine.standing_queries.loader import all_definitions


def _callback_url() -> str:
    from modok.webhook.server import load_config as _load_webhook_config

    try:
        webhook_config = _load_webhook_config()
    except Exception:
        webhook_config = None
    host = webhook_config.host if webhook_config else "127.0.0.1"
    port = webhook_config.port if webhook_config else 4242
    return f"http://{host}:{port}/standing-query/result"


@click.group("stream")
def stream_cmd() -> None:
    """Manage Quine standing queries."""


@stream_cmd.command("install")
def stream_install() -> None:
    config = ModokConfig.load()
    client = QuineClient(base_url=config.quine.url)
    callback_url = _callback_url()

    try:
        for definition in all_definitions():
            installed = asyncio.run(client.install_standing_query(definition, callback_url))
            if installed:
                click.echo(f"{definition.name}: installed")
            else:
                click.echo(f"{definition.name}: already installed")
    except Exception as exc:
        click.echo(f"Could not reach Quine at {config.quine.url}: {exc}", err=True)
        raise SystemExit(2)


@stream_cmd.command("status")
def stream_status() -> None:
    config = ModokConfig.load()
    client = QuineClient(base_url=config.quine.url)

    try:
        names = asyncio.run(client.list_standing_queries())
    except Exception as exc:
        click.echo(f"Could not reach Quine at {config.quine.url}: {exc}", err=True)
        raise SystemExit(2)

    if not names:
        click.echo("(no standing queries installed)")
    for name in names:
        click.echo(name)


@stream_cmd.command("remove")
def stream_remove() -> None:
    config = ModokConfig.load()
    client = QuineClient(base_url=config.quine.url)

    try:
        for definition in all_definitions():
            removed = asyncio.run(client.remove_standing_query(definition.name))
            if removed:
                click.echo(f"{definition.name}: removed")
            else:
                click.echo(f"{definition.name}: not installed")
    except Exception as exc:
        click.echo(f"Could not reach Quine at {config.quine.url}: {exc}", err=True)
        raise SystemExit(2)
