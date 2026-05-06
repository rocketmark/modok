"""modok search command — substring search across graph properties."""

from __future__ import annotations

import asyncio
import json

import click

from modok.cli.config import ModokConfig
from modok.cli.commands._output import collect_nodes, dedup_nodes, print_node, require_quine

_HEADING_CYPHER = """
MATCH (n)
WHERE n.project_slug = $project_slug
  AND n.node_type = 'DocSection'
  AND toLower(n.heading_text) CONTAINS toLower($query)
RETURN n
ORDER BY n.doc_path, n.line_start
"""

_TEXT_CYPHER = """
MATCH (n)
WHERE n.project_slug = $project_slug
  AND (
    toLower(coalesce(n.heading_text, '')) CONTAINS toLower($query)
    OR toLower(coalesce(n.name, '')) CONTAINS toLower($query)
    OR toLower(coalesce(n.summary, '')) CONTAINS toLower($query)
    OR toLower(coalesce(n.normalized_error, '')) CONTAINS toLower($query)
    OR toLower(coalesce(n.module_slug, '')) CONTAINS toLower($query)
    OR toLower(coalesce(n.feature_slug, '')) CONTAINS toLower($query)
    OR toLower(coalesce(n.repo_path, '')) CONTAINS toLower($query)
  )
RETURN n
"""


@click.command("search")
@click.option("--project", required=True, help="Project slug.")
@click.option("--section", default=None, help="Substring match against doc section headings.")
@click.option(
    "--text", "text_query", default=None, help="Substring match across all node properties."
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON.")
@click.argument("query", required=False, default=None)
def search_cmd(
    project: str,
    section: str | None,
    text_query: str | None,
    as_json: bool,
    query: str | None,
) -> None:
    """Search the graph by keyword. QUERY argument is shorthand for --text."""
    if query and text_query:
        raise click.ClickException("Supply QUERY argument or --text, not both.")

    effective_text = text_query or query

    if not any([section, effective_text]):
        raise click.ClickException("Supply a QUERY argument, --section, or --text.")

    config = ModokConfig.load()
    config.project(project)

    client = require_quine(config)

    nodes = []

    if section:
        rows = asyncio.run(
            client.query(_HEADING_CYPHER, {"project_slug": project, "query": section})
        )
        nodes.extend(collect_nodes(rows))

    if effective_text:
        rows = asyncio.run(
            client.query(_TEXT_CYPHER, {"project_slug": project, "query": effective_text})
        )
        nodes.extend(collect_nodes(rows))

    unique = dedup_nodes(nodes)

    if as_json:
        click.echo(json.dumps({"project": project, "nodes": unique}))
    else:
        _print_tabular(project, unique, section=section, text_query=effective_text)


def _print_tabular(
    project: str,
    nodes: list,
    *,
    section: str | None,
    text_query: str | None,
) -> None:
    parts = []
    if section:
        parts.append(f"section~{section!r}")
    if text_query:
        parts.append(f"text~{text_query!r}")
    click.echo(f"Project: {project}  Search: {', '.join(parts)}")

    if not nodes:
        click.echo("  (no results)")
        return

    for node in nodes:
        print_node(node)
