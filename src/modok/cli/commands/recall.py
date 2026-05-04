"""modok recall command."""
# @spec CLI-REC-001, CLI-REC-002, CLI-REC-003, CLI-REC-004, CLI-REC-005

from __future__ import annotations

import asyncio
import json

import click

from modok.cli.config import ModokConfig
from modok.quine.client import QuineClient

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

    client = QuineClient(base_url=config.quine.url)
    if not asyncio.run(client.ping()):
        click.echo(
            f"Quine is not reachable at {config.quine.url} — run `modok quine start` or check your config",
            err=True,
        )
        raise SystemExit(2)

    nodes = []

    if feature:
        rows = asyncio.run(client.query(_FEATURE_CYPHER, {"project_slug": project, "feature_slug": feature}))
        nodes.extend(_collect(rows))

    if module_slug:
        rows = asyncio.run(client.query(_MODULE_CYPHER, {"project_slug": project, "module_slug": module_slug}))
        nodes.extend(_collect(rows))

    seen: set[str] = set()
    unique: list[dict] = []
    for n in nodes:
        nid = n.get("id") if isinstance(n, dict) else None
        if nid not in seen:
            seen.add(nid)
            unique.append(n)

    if as_json:
        click.echo(json.dumps({"project": project, "nodes": unique}))
    else:
        _print_tabular(project, unique, feature=feature, module_slug=module_slug)


def _collect(rows: list) -> list[dict]:
    out = []
    for row in rows:
        for item in row:
            if item is not None and isinstance(item, dict):
                out.append(item)
    return out


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
        props = node.get("properties", node)
        node_type = props.get("node_type", "Node")
        if node_type == "DocSection":
            click.echo(f"  [DocSection] {props.get('doc_path', '')}:{props.get('line_start', '')}  {props.get('heading_text', '')}")
        elif node_type == "Module":
            click.echo(f"  [Module] {props.get('module_slug', '')}  {props.get('name', '')}")
        elif node_type == "File":
            click.echo(f"  [File] {props.get('repo_path', '')}")
        elif node_type == "TestFile":
            click.echo(f"  [TestFile] {props.get('repo_path', '')}")
        elif node_type == "Feature":
            click.echo(f"  [Feature] {props.get('feature_slug', '')}  {props.get('name', '')}")
        elif node_type == "KnownIssue":
            click.echo(f"  [KnownIssue] {props.get('issue_id', '')}  {props.get('summary', '')}  [{props.get('status', '')}]")
        elif node_type == "ErrorSignature":
            click.echo(f"  [ErrorSignature] {props.get('normalized_error', '')}  {props.get('display_text', '')}")
        elif node_type == "Fix":
            click.echo(f"  [Fix] {props.get('fix_id', '')}  {props.get('summary', '')}  [{props.get('kind', '')}]")
        else:
            click.echo(f"  [{node_type}] {props}")
