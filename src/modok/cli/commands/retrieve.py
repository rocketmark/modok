"""modok retrieve command."""
# @spec CLI-RET-001, CLI-RET-002, CLI-RET-003, CLI-RET-004, CLI-RET-005, CLI-RET-006, CLI-RET-007, CLI-RET-008, CLI-RET-009

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys

import click

from modok.cli.config import ModokConfig
from modok.ingestion.registry import Registry
from modok.quine.client import QuineClient
from modok.retrieval.engine import retrieve
from modok.retrieval.errors import (
    DREAnchorError,
    DREGraphUnavailableError,
    DRELLMUnavailableError,
    DRENotFoundError,
)


@click.command("retrieve")
@click.option("--project", required=True, help="Project slug.")
@click.option("--ticket", default=None, help="Ticket ID.")
@click.option("--node-id", "node_id", default=None, type=int, help="Quine node ID (power-user).")
@click.option("--stream", "stream_mode", is_flag=True, default=False, help="Emit NDJSON progress lines before the final result.")
def retrieve_cmd(project: str, ticket: str | None, node_id: int | None, stream_mode: bool) -> None:
    has_ticket = ticket is not None
    has_node_id = node_id is not None

    if has_ticket and has_node_id:
        raise click.ClickException("--ticket and --node-id are mutually exclusive.")

    if not has_ticket and not has_node_id:
        raise click.ClickException("Supply --ticket or --node-id.")

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
        module_source_files = registry.all_module_source_files()
    except Exception:
        feature_slugs = None
        module_slugs = None
        valid_slugs = None
        feature_descriptions = None
        module_descriptions = None
        module_elements = None
        module_source_files = None

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
        # Look up the CustomerIssue node by (project_slug, ticket_id).
        # NOTE: This assumes ticket IDs are unique within a project. If multi-source
        # disambiguation is needed in future (e.g. Zendesk + Jira sharing IDs),
        # add a --source flag and switch back to idFrom('customer-issue', p, source, t).
        rows = asyncio.run(client.query(
            "MATCH (n) WHERE n.project_slug = $p AND n.ticket_id = $t RETURN id(n) LIMIT 1",
            {"p": project, "t": ticket},
        ))
        if not rows or not rows[0]:
            raise click.ClickException(f"issue not found in project `{project}`")
        resolved_id = rows[0][0]

    def _on_progress(step: str, partial) -> None:
        sys.stdout.write(json.dumps({"step": step, "data": dataclasses.asdict(partial)}) + "\n")
        sys.stdout.flush()

    try:
        packet = asyncio.run(retrieve(
            resolved_id, project, client,
            valid_slugs=valid_slugs,
            feature_slugs=feature_slugs,
            module_slugs=module_slugs,
            feature_descriptions=feature_descriptions,
            module_descriptions=module_descriptions,
            module_elements=module_elements,
            module_source_files=module_source_files,
            on_progress=_on_progress if stream_mode else None,
            skip_summary=config.llm.skip_summary,
        ))
    except DRENotFoundError:
        raise click.ClickException(f"issue not found in project `{project}`")
    except DREAnchorError as exc:
        raise click.ClickException(f"anchor extraction failed: {exc}")
    except (DREGraphUnavailableError, DRELLMUnavailableError):
        raise SystemExit(2)

    if stream_mode:
        sys.stdout.write(json.dumps({"step": "complete", "data": dataclasses.asdict(packet)}) + "\n")
        sys.stdout.flush()
    else:
        click.echo(json.dumps(dataclasses.asdict(packet)))
