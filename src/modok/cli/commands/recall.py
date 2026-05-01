"""modok recall command."""
# @spec CLI-REC-001, CLI-REC-002, CLI-REC-003, CLI-REC-004, CLI-REC-005

from __future__ import annotations

import asyncio
import json

import click

from modok.cli.config import ModokConfig
from modok.quine.client import QuineClient

_RECALL_CYPHER = """
MATCH (f:Feature {project_slug: $project_slug, feature_slug: $feature_slug})
OPTIONAL MATCH (f)-[]->(n)
RETURN n
"""


@click.command("recall")
@click.option("--project", required=True, help="Project slug.")
@click.option("--feature", required=True, help="Feature slug.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON.")
def recall_cmd(project: str, feature: str, as_json: bool) -> None:
    config = ModokConfig.load()
    config.project(project)

    client = QuineClient(base_url=config.quine.url)
    if not asyncio.get_event_loop().run_until_complete(client.ping()):
        click.echo(
            f"Quine is not reachable at {config.quine.url} — run `modok quine start` or check your config",
            err=True,
        )
        raise SystemExit(2)

    rows = asyncio.get_event_loop().run_until_complete(
        client.query(_RECALL_CYPHER, {"project_slug": project, "feature_slug": feature})
    )

    nodes = [row[0] for row in rows if row and row[0] is not None]

    if as_json:
        click.echo(json.dumps({"feature": feature, "project": project, "nodes": nodes}))
    else:
        _print_tabular(feature, project, nodes)


def _print_tabular(feature: str, project: str, nodes: list) -> None:
    click.echo(f"Feature: {feature}  Project: {project}")
    if not nodes:
        click.echo("  (no results)")
        return
    for node in nodes:
        if isinstance(node, dict):
            props = node.get("properties", node)
            node_type = props.get("node_type", "Node")
            click.echo(f"  [{node_type}] {props}")
        else:
            click.echo(f"  {node}")
