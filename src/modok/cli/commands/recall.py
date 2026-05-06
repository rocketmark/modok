"""modok recall command."""
# @spec CLI-REC-001, CLI-REC-002, CLI-REC-003, CLI-REC-004, CLI-REC-005

from __future__ import annotations

import asyncio
import json

import click

from modok.cli.config import ModokConfig
from modok.cli.commands._output import collect_nodes, dedup_nodes, print_node, require_quine

_FEATURE_CYPHER = """
MATCH (f) WHERE id(f) = idFrom('feature', $project_slug, $feature_slug)
OPTIONAL MATCH (f)-[]->(n)
RETURN f, n
"""

_MODULE_CYPHER = """
MATCH (m) WHERE id(m) = idFrom('module', $project_slug, $module_slug)
OPTIONAL MATCH (f)-[:IMPLEMENTED_BY]->(m)
OPTIONAL MATCH (m)-[:DEFINED_IN]->(file)
OPTIONAL MATCH (f)-[:HAS_TEST]->(tfile)
RETURN m, f, file, tfile
"""


@click.command("recall")
@click.option("--project", required=True, help="Project slug.")
@click.option("--feature", default=None, help="Feature slug.")
@click.option("--module", "module_slug", default=None, help="Module slug.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON.")
def recall_cmd(
    project: str,
    feature: str | None,
    module_slug: str | None,
    as_json: bool,
) -> None:
    if not any([feature, module_slug]):
        raise click.ClickException("Supply at least one of --feature or --module.")

    config = ModokConfig.load()
    config.project(project)

    client = require_quine(config)

    nodes = []

    if feature:
        rows = asyncio.run(
            client.query(_FEATURE_CYPHER, {"project_slug": project, "feature_slug": feature})
        )
        nodes.extend(collect_nodes(rows))

    if module_slug:
        rows = asyncio.run(
            client.query(_MODULE_CYPHER, {"project_slug": project, "module_slug": module_slug})
        )
        nodes.extend(collect_nodes(rows))

    unique = dedup_nodes(nodes)

    if as_json:
        click.echo(json.dumps({"project": project, "nodes": unique}))
    else:
        _print_tabular(project, unique, feature=feature, module_slug=module_slug)


def _print_tabular(
    project: str,
    nodes: list,
    *,
    feature: str | None,
    module_slug: str | None,
) -> None:
    parts = []
    if feature:
        parts.append(f"feature={feature}")
    if module_slug:
        parts.append(f"module={module_slug}")
    click.echo(f"Project: {project}  Query: {', '.join(parts)}")

    if not nodes:
        click.echo("  (no results)")
        return

    for node in nodes:
        print_node(node)
