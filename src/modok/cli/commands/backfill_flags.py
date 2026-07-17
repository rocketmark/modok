"""modok backfill-flags command — one-time catch-up for CustomerIssue nodes
whose one-time investigation already fired before FileEscalation/
RootCauseEscalation's FLAGS write-back existed, so they still count toward
those patterns' thresholds. Found live: a standing query's DistinctId keying
fires at most once per ticket, ever — a ticket already investigated under
older code never gets a second chance to have retrieve()/FLAGS computed
through the normal write-back path (docs/llds/file-escalation-pattern.md
§ FLAGS Write-Back). Safe to re-run: skips any ticket that already has a
FLAGS edge."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import click

from modok.cli.commands._output import require_quine
from modok.cli.config import ModokConfig
from modok.ingestion.registry import Registry
from modok.quine.client import QuineClient
from modok.retrieval.engine import retrieve, write_flags_for_packet


async def _backfill_created_at_if_missing(client, project_slug: str, ticket_id: str) -> None:
    rows = await client.query(
        "MATCH (ci) WHERE ci.node_type = 'CustomerIssue' AND ci.project_slug = $p "
        "AND ci.source_system = 'github' AND ci.ticket_id = $t RETURN ci.created_at",
        {"p": project_slug, "t": ticket_id},
    )
    current = rows[0][0] if rows and rows[0] else None
    if current:
        return

    inv_rows = await client.query(
        "MATCH (inv)-[:INVESTIGATES]->(ci) WHERE ci.node_type = 'CustomerIssue' "
        "AND ci.project_slug = $p AND ci.source_system = 'github' AND ci.ticket_id = $t "
        "RETURN inv.triggered_at ORDER BY inv.triggered_at ASC LIMIT 1",
        {"p": project_slug, "t": ticket_id},
    )
    if inv_rows and inv_rows[0] and inv_rows[0][0]:
        proxy = inv_rows[0][0]
    else:
        proxy = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    await client.query(
        "MATCH (ci) WHERE id(ci) = idFrom('customer-issue', $p, 'github', $t) SET ci.created_at = $ts",
        {"p": project_slug, "t": ticket_id, "ts": proxy},
    )


@click.command("backfill-flags")
@click.option("--project", required=True, help="Project slug.")
def backfill_flags_cmd(project: str) -> None:
    """Compute FLAGS edges (and backfill created_at where missing) for open
    GitHub tickets that predate the File/Root-Cause Escalation write-back.
    Safe to re-run — skips tickets that already have a FLAGS edge."""
    config = ModokConfig.load()
    proj = config.project(project)
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
        feature_source_files = registry.all_feature_source_files()
    except Exception:
        feature_slugs = module_slugs = valid_slugs = None
        feature_descriptions = module_descriptions = None
        module_elements = module_source_files = feature_source_files = None

    client = require_quine(config, QuineClient)

    async def _run() -> None:
        open_rows = await client.query(
            "MATCH (ci) WHERE ci.node_type = 'CustomerIssue' AND ci.project_slug = $p "
            "AND ci.source_system = 'github' AND ci.status = 'open' "
            "RETURN ci.ticket_id AS ticket_id",
            {"p": project},
        )
        flagged_rows = await client.query(
            "MATCH (ci)-[:FLAGS]->() WHERE ci.node_type = 'CustomerIssue' AND ci.project_slug = $p "
            "RETURN DISTINCT ci.ticket_id AS ticket_id",
            {"p": project},
        )
        already_flagged = {row[0] for row in flagged_rows}
        pending = [row[0] for row in open_rows if row[0] not in already_flagged]

        if not pending:
            click.echo("Nothing to backfill — every open GitHub ticket already has FLAGS computed.")
            return

        for i, ticket_id in enumerate(pending, start=1):
            click.echo(f"[{i}/{len(pending)}] ticket {ticket_id}...", err=True)

            id_rows = await client.query(
                "MATCH (n) WHERE n.node_type = 'CustomerIssue' AND n.project_slug = $p "
                "AND n.ticket_id = $t RETURN id(n) LIMIT 1",
                {"p": project, "t": ticket_id},
            )
            if not id_rows or not id_rows[0]:
                click.echo("  skipped (node not found)", err=True)
                continue
            node_id = id_rows[0][0]

            try:
                packet = await retrieve(
                    node_id,
                    project,
                    client,
                    valid_slugs=valid_slugs,
                    feature_slugs=feature_slugs,
                    module_slugs=module_slugs,
                    feature_descriptions=feature_descriptions,
                    module_descriptions=module_descriptions,
                    module_elements=module_elements,
                    module_source_files=module_source_files,
                    feature_source_files=feature_source_files,
                    skip_summary=True,  # backfill only needs scored_candidates
                )
            except Exception as exc:
                click.echo(f"  skipped (retrieve failed: {exc})", err=True)
                continue

            flagged = await write_flags_for_packet(client, project, "github", ticket_id, packet)
            await _backfill_created_at_if_missing(client, project, ticket_id)
            click.echo(f"  flagged: {flagged or '(none — no high-confidence source candidates)'}")

    asyncio.run(_run())
