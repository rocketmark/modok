"""modok diagnose command — feature-anchored debug packet assembly."""
# @spec CLI-DIAG-001, CLI-DIAG-002, CLI-DIAG-003, CLI-DIAG-004, CLI-DIAG-005,
#       CLI-DIAG-006, CLI-DIAG-007, CLI-DIAG-008, CLI-DIAG-009, CLI-DIAG-010,
#       CLI-DIAG-011, CLI-DIAG-012, CLI-DIAG-013

from __future__ import annotations

import asyncio
import dataclasses
import json

import click

from modok.cli.config import ModokConfig
from modok.quine.client import QuineClient
from modok.retrieval.models import (
    AnchorSet,
    DebugPacket,
    EvidenceAnchor,
    FileRef,
    FixRef,
    KnownIssueRef,
)

_KI_CAP = 10
_FIX_CAP = 10
_FILE_CAP = 20

_FILES_CYPHER = """
MATCH (f)
WHERE f.project_slug = $project_slug AND f.feature_slug = $feature_slug AND f.node_type = 'Feature'
MATCH (f)-[:IMPLEMENTED_BY]->(m)-[:DEFINED_IN]->(file)
WHERE file.node_type = 'File'
RETURN file
"""

_KI_CYPHER = """
MATCH (f)
WHERE f.project_slug = $project_slug AND f.feature_slug = $feature_slug AND f.node_type = 'Feature'
MATCH (f)-[:HAS_KNOWN_ISSUE]->(ki)
WHERE ki.node_type = 'KnownIssue'
RETURN ki
"""

_ERROR_KI_CYPHER = """
MATCH (ki)
WHERE ki.node_type = 'KnownIssue' AND ki.project_slug = $project_slug
MATCH (ki)-[:HAS_ERROR]->(e)
WHERE e.node_type = 'ErrorSignature' AND e.normalized_error = $normalized_error
RETURN ki
"""

_FIXES_CYPHER = """
MATCH (ki)
WHERE id(ki) = $ki_node_id
MATCH (ki)-[:RESOLVED_BY]->(fix)
WHERE fix.node_type = 'Fix'
RETURN fix
"""


def _accumulate(counts: dict[str, int], key: str, delta: int = 1) -> None:
    counts[key] = counts.get(key, 0) + delta


def _sort_cap(items: list[dict], cap: int) -> list[dict]:
    return sorted(items, key=lambda x: x["match_count"], reverse=True)[:cap]


async def _run_diagnose(
    project: str,
    feature: str,
    error: str | None,
    symptom: str | None,
    client: QuineClient,
) -> DebugPacket:
    ki_counts: dict[str, int] = {}
    ki_meta: dict[str, dict] = {}
    ki_node_ids: dict[str, str] = {}
    fix_counts: dict[str, int] = {}
    fix_meta: dict[str, dict] = {}
    file_counts: dict[str, int] = {}

    # Files via feature → module → file
    rows = await client.query(_FILES_CYPHER, {"project_slug": project, "feature_slug": feature})
    for row in rows:
        for item in row:
            if isinstance(item, dict) and item.get("properties", {}).get("repo_path"):
                _accumulate(file_counts, item["properties"]["repo_path"])

    # KnownIssues via feature → HAS_KNOWN_ISSUE
    rows = await client.query(_KI_CYPHER, {"project_slug": project, "feature_slug": feature})
    for row in rows:
        for item in row:
            if not isinstance(item, dict):
                continue
            props = item.get("properties", {})
            ki_id = props.get("issue_id")
            if not ki_id:
                continue
            if symptom and symptom.lower() not in props.get("summary", "").lower():
                continue
            _accumulate(ki_counts, ki_id)
            ki_meta[ki_id] = props
            ki_node_ids[ki_id] = item["id"]

    # Additional KnownIssues via error signature
    if error:
        rows = await client.query(_ERROR_KI_CYPHER, {"project_slug": project, "normalized_error": error})
        for row in rows:
            for item in row:
                if not isinstance(item, dict):
                    continue
                props = item.get("properties", {})
                ki_id = props.get("issue_id")
                if not ki_id:
                    continue
                if symptom and symptom.lower() not in props.get("summary", "").lower():
                    continue
                _accumulate(ki_counts, ki_id)
                ki_meta.setdefault(ki_id, props)
                ki_node_ids.setdefault(ki_id, item["id"])

    # Fixes via KnownIssue → RESOLVED_BY
    for ki_id, node_id in ki_node_ids.items():
        rows = await client.query(_FIXES_CYPHER, {"ki_node_id": node_id})
        for row in rows:
            for item in row:
                if not isinstance(item, dict):
                    continue
                props = item.get("properties", {})
                fix_id = props.get("fix_id")
                if fix_id:
                    _accumulate(fix_counts, fix_id)
                    fix_meta.setdefault(fix_id, props)

    ki_items = _sort_cap([{"id": k, "match_count": v} for k, v in ki_counts.items()], _KI_CAP)
    fix_items = _sort_cap([{"id": k, "match_count": v} for k, v in fix_counts.items()], _FIX_CAP)
    file_items = _sort_cap([{"id": k, "match_count": v} for k, v in file_counts.items()], _FILE_CAP)

    return DebugPacket(
        issue_summary=f"diagnose: {feature}",
        anchors=AnchorSet(
            feature_slugs=[feature],
            error_signatures=[error] if error else [],
            symptoms=[symptom] if symptom else [],
        ),
        anchor_count=1 + (1 if error else 0),
        known_issues=[
            KnownIssueRef(
                known_issue_id=item["id"],
                summary=ki_meta[item["id"]].get("summary", ""),
                status=ki_meta[item["id"]].get("status", ""),
                match_count=item["match_count"],
            )
            for item in ki_items
        ],
        recent_fixes=[
            FixRef(
                fix_id=item["id"],
                summary=fix_meta[item["id"]].get("summary", ""),
                kind=fix_meta[item["id"]].get("kind", ""),
                match_count=item["match_count"],
            )
            for item in fix_items
        ],
        relevant_files=[
            FileRef(repo_path=item["id"], match_count=item["match_count"])
            for item in file_items
        ],
        evidence=[
            EvidenceAnchor(anchor_type="feature", anchor_value=feature),
        ],
        confidence=1.0,
    )


@click.command("diagnose")
@click.option("--project", required=True, help="Project slug.")
@click.option("--feature", required=True, help="Feature slug to anchor the traversal.")
@click.option("--error", default=None, help="Error signature slug (normalized_error) to narrow results.")
@click.option("--symptom", default=None, help="Substring match against KnownIssue summaries.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON.")
def diagnose_cmd(
    project: str,
    feature: str,
    error: str | None,
    symptom: str | None,
    as_json: bool,
) -> None:
    """Assemble a debug packet anchored on a feature slug."""
    config = ModokConfig.load()
    config.project(project)

    client = QuineClient(base_url=config.quine.url)
    if not asyncio.run(client.ping()):
        click.echo(
            f"Quine is not reachable at {config.quine.url} — run `modok quine start` or check your config",
            err=True,
        )
        raise SystemExit(2)

    packet = asyncio.run(_run_diagnose(project, feature, error, symptom, client))

    if as_json:
        click.echo(json.dumps(dataclasses.asdict(packet)))
    else:
        _print_packet(packet)


def _print_packet(packet: DebugPacket) -> None:
    click.echo(f"Feature: {packet.anchors.feature_slugs[0]}")
    if packet.anchors.error_signatures:
        click.echo(f"Error:   {packet.anchors.error_signatures[0]}")
    if packet.anchors.symptoms:
        click.echo(f"Symptom: {packet.anchors.symptoms[0]}")
    click.echo("")

    if not packet.known_issues and not packet.recent_fixes and not packet.relevant_files:
        click.echo("  (no results)")
        return

    if packet.known_issues:
        click.echo("Known Issues:")
        for ki in packet.known_issues:
            click.echo(f"  [{ki.status}] {ki.known_issue_id}  {ki.summary}")

    if packet.recent_fixes:
        click.echo("Fixes:")
        for fix in packet.recent_fixes:
            click.echo(f"  [{fix.kind}] {fix.fix_id}  {fix.summary}")

    if packet.relevant_files:
        click.echo("Files:")
        for f in packet.relevant_files:
            click.echo(f"  {f.repo_path}")
