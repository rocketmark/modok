"""Shared CLI output helpers for graph node display."""

from __future__ import annotations

import asyncio

import click


def require_quine(config):
    """Return a connected QuineClient or print an error and exit with code 2."""
    from modok.quine.client import QuineClient
    client = QuineClient(base_url=config.quine.url)
    if not asyncio.run(client.ping()):
        click.echo(
            f"Quine is not reachable at {config.quine.url} — run `modok quine start` or check your config",
            err=True,
        )
        raise SystemExit(2)
    return client


def collect_nodes(rows: list) -> list[dict]:
    out = []
    for row in rows:
        for item in row:
            if item is not None and isinstance(item, dict):
                out.append(item)
    return out


def dedup_nodes(nodes: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique: list[dict] = []
    for n in nodes:
        nid = n.get("id") if isinstance(n, dict) else None
        if nid not in seen:
            seen.add(nid)
            unique.append(n)
    return unique


def print_node(node: dict) -> None:
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
