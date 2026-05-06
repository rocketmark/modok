"""modok ingest command."""
# @spec CLI-INGEST-001, CLI-INGEST-002, CLI-INGEST-003, CLI-INGEST-005, CLI-PING-001

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import click

from modok.cli.config import ModokConfig
from modok.cli.commands._output import require_quine
from modok.ingestion.pipeline import run_ingestion
from modok.ingestion.registry import Registry
from modok.quine.client import QuineClient
from modok.quine.models import CustomerIssue


@click.command("ingest")
@click.option("--project", required=True, help="Project slug.")
@click.argument("ticket_file", required=False, default=None)
def ingest_cmd(project: str, ticket_file: str | None) -> None:
    config = ModokConfig.load()
    proj = config.project(project)

    client = require_quine(config)

    if ticket_file is not None:
        _ingest_customer_ticket(Path(ticket_file), project, client)
        return

    repo_root = Path(proj.repo)
    registry = Registry(repo_root)
    report = asyncio.run(run_ingestion(
        repo_root=repo_root,
        registry=registry,
        client=client,
        project_slug=project,
    ))
    click.echo(str(report))
    if report.errors:
        raise SystemExit(3)


def _ingest_customer_ticket(path: Path, project_slug: str, client: QuineClient) -> None:
    """Parse a customer ticket markdown file and upsert it as a CustomerIssue node."""
    text = path.read_text(encoding="utf-8")

    # Extract ticket ID from the first heading: "# Ticket: <id>"
    id_match = re.search(r"^# Ticket:\s*(.+)$", text, re.MULTILINE)
    if not id_match:
        click.echo(f"Could not parse ticket ID from {path}", err=True)
        raise SystemExit(1)
    ticket_id = id_match.group(1).strip()

    # Extract source from "**Source:** <value>"
    source_match = re.search(r"^\*\*Source:\*\*\s*(.+)$", text, re.MULTILINE)
    source_system = source_match.group(1).strip() if source_match else "unknown"

    # Extract subject from the "## Subject" section
    subject_match = re.search(r"^## Subject\s*\n+(.+?)(?=\n##|\Z)", text, re.MULTILINE | re.DOTALL)
    summary = subject_match.group(1).strip() if subject_match else ticket_id

    node = CustomerIssue(
        node_type="CustomerIssue",
        project_slug=project_slug,
        source_system=source_system,
        ticket_id=ticket_id,
        summary=summary,
        raw_text=text,
        status="open",
    )
    asyncio.run(client.upsert_node(node))
    click.echo(f"Ingested customer ticket {ticket_id} (source: {source_system})")


