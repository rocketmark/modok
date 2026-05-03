"""modok retrieve command."""
# @spec CLI-RET-001, CLI-RET-002, CLI-RET-003, CLI-RET-004, CLI-RET-005, CLI-RET-006, CLI-RET-007, CLI-RET-008, CLI-RET-009

from __future__ import annotations

import asyncio
import dataclasses
import json

import click

from modok.cli.config import ModokConfig
from modok.ingestion.registry import Registry
from modok.quine.client import QuineClient
from modok.retrieval.engine import retrieve
from modok.retrieval.errors import (
    DREGraphUnavailableError,
    DRELLMUnavailableError,
    DRENotFoundError,
)


@click.command("retrieve")
@click.option("--project", required=True, help="Project slug.")
@click.option("--source", default=None, help="Source system (e.g. zendesk).")
@click.option("--ticket", default=None, help="Ticket ID.")
@click.option("--node-id", "node_id", default=None, type=int, help="Quine node ID (power-user).")
def retrieve_cmd(project: str, source: str | None, ticket: str | None, node_id: int | None) -> None:
    # Mutual exclusivity checks (before any graph operation)
    has_source_ticket = source is not None or ticket is not None
    has_node_id = node_id is not None

    if has_source_ticket and has_node_id:
        raise click.ClickException("--source/--ticket and --node-id are mutually exclusive.")

    if not has_source_ticket and not has_node_id:
        raise click.ClickException("Supply --source and --ticket, or --node-id.")

    if has_source_ticket and (source is None or ticket is None):
        raise click.ClickException("--source and --ticket must both be supplied together.")

    config = ModokConfig.load()
    proj = config.project(project)  # validates slug; raises ClickException if unknown

    from pathlib import Path
    repo_root = Path(proj.repo)
    try:
        registry = Registry(repo_root)
        feature_slugs = registry.feature_slugs()
        module_slugs = registry.module_slugs()
        valid_slugs = feature_slugs + module_slugs
        feature_descriptions = registry.feature_descriptions()
        module_descriptions = registry.module_descriptions()
        module_elements = registry.module_elements()
    except Exception:
        feature_slugs = None
        module_slugs = None
        valid_slugs = None
        feature_descriptions = None
        module_descriptions = None
        module_elements = None

    client = QuineClient(base_url=config.quine.url)
    if not asyncio.run(client.ping()):
        click.echo(
            f"Quine is not reachable at {config.quine.url} — run `modok quine start` or check your config",
            err=True,
        )
        raise SystemExit(2)

    if has_node_id:
        resolved_id = str(node_id)
    else:
        # Resolve via Quine's native idFrom() — returns a UUID string, not a Python int.
        # The Python ids.idFrom is a test-harness stub that uses a different algorithm.
        rows = asyncio.run(client.query(
            "RETURN idFrom('customer-issue', $p, $s, $t)",
            {"p": project, "s": source, "t": ticket},
        ))
        resolved_id = rows[0][0]

    try:
        packet = asyncio.run(retrieve(
            resolved_id, project, client,
            valid_slugs=valid_slugs,
            feature_slugs=feature_slugs,
            module_slugs=module_slugs,
            feature_descriptions=feature_descriptions,
            module_descriptions=module_descriptions,
            module_elements=module_elements,
        ))
    except DRENotFoundError:
        raise click.ClickException(f"issue not found in project `{project}`")
    except (DREGraphUnavailableError, DRELLMUnavailableError):
        raise SystemExit(2)

    click.echo(json.dumps(dataclasses.asdict(packet)))
