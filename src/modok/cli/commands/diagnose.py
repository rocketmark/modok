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
from modok.cli.commands._output import require_quine
from modok.quine.client import QuineClient
from modok.retrieval.engine import (
    _KI_CAP,
    _FIX_CAP,
    _FILE_CAP,
    _accumulate_match_count,
    _sort_and_cap,
)
from modok.retrieval.models import (
    AffectedArea,
    DebugPacket,
    IssueAnchors,
    IssueSummary,
    KnownIssueRef,
    PriorFix,
)

_FILES_CYPHER = """
MATCH (f)
WHERE f.project_slug = $project_slug AND f.feature_slug = $feature_slug AND f.node_type = 'Feature'
MATCH (f)-[:IMPLEMENTED_BY]->(m)-[:DEFINED_IN]->(file)
WHERE file.node_type = 'File'
RETURN file
"""

_TEST_FILES_CYPHER = """
MATCH (f)
WHERE f.project_slug = $project_slug AND f.feature_slug = $feature_slug AND f.node_type = 'Feature'
MATCH (f)-[:HAS_TEST]->(file)
WHERE file.node_type = 'TestFile'
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

_FIX_COMMIT_CYPHER = """
MATCH (f)
WHERE id(f) = idFrom('fix', $project_slug, $fix_id)
MATCH (f)-[:IMPLEMENTED_IN]->(c:Commit)
RETURN c
"""


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
    test_file_counts: dict[str, int] = {}

    # Source files via feature → module → DEFINED_IN → file
    rows = await client.query(_FILES_CYPHER, {"project_slug": project, "feature_slug": feature})
    for row in rows:
        for item in row:
            if isinstance(item, dict) and item.get("properties", {}).get("repo_path"):
                _accumulate_match_count(file_counts, item["properties"]["repo_path"], 1)

    # Test files via feature → HAS_TEST → file
    rows = await client.query(
        _TEST_FILES_CYPHER, {"project_slug": project, "feature_slug": feature}
    )
    for row in rows:
        for item in row:
            if isinstance(item, dict) and item.get("properties", {}).get("repo_path"):
                _accumulate_match_count(test_file_counts, item["properties"]["repo_path"], 1)

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
            _accumulate_match_count(ki_counts, ki_id, 1)
            ki_meta[ki_id] = props
            ki_node_ids[ki_id] = item["id"]

    # Additional KnownIssues via error signature
    if error:
        rows = await client.query(
            _ERROR_KI_CYPHER, {"project_slug": project, "normalized_error": error}
        )
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
                _accumulate_match_count(ki_counts, ki_id, 1)
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
                    _accumulate_match_count(fix_counts, fix_id, 1)
                    fix_meta.setdefault(fix_id, props)

    ki_items = _sort_and_cap([{"id": k, "match_count": v} for k, v in ki_counts.items()], _KI_CAP)
    fix_items = _sort_and_cap(
        [{"id": k, "match_count": v} for k, v in fix_counts.items()], _FIX_CAP
    )
    file_items = _sort_and_cap(
        [{"id": k, "match_count": v} for k, v in file_counts.items()], _FILE_CAP
    )
    test_items = _sort_and_cap(
        [{"id": k, "match_count": v} for k, v in test_file_counts.items()], _FILE_CAP
    )

    # Fetch commit SHAs for fixes (best-effort)
    prior_fixes: list[PriorFix] = []
    for item in fix_items:
        fid = item["id"]
        commit_sha = ""
        try:
            rows = await client.query(_FIX_COMMIT_CYPHER, {"project_slug": project, "fix_id": fid})
            for row in rows:
                if row and row[0] and isinstance(row[0], dict):
                    sha = row[0].get("properties", {}).get("sha", "")
                    if sha:
                        commit_sha = str(sha)[:7]
                        break
        except Exception:
            pass
        prior_fixes.append(
            PriorFix(
                id=fid,
                commit=commit_sha,
                summary=fix_meta[fid].get("summary", ""),
            )
        )

    return DebugPacket(
        issue=IssueSummary(
            summary=f"diagnose: {feature}",
            anchors=IssueAnchors(
                features=[feature],
                errors=[error] if error else [],
                symptoms=[symptom] if symptom else [],
            ),
        ),
        affected_areas=[AffectedArea(type="feature", id=f"feature:{feature}", name=feature)],
        relevant_files=[item["id"] for item in file_items],
        relevant_tests=[item["id"] for item in test_items],
        known_issues=[
            KnownIssueRef(
                id=item["id"],
                summary=ki_meta[item["id"]].get("summary", ""),
            )
            for item in ki_items
        ],
        prior_fixes=prior_fixes,
    )


@click.command("diagnose")
@click.option("--project", required=True, help="Project slug.")
@click.option("--feature", required=True, help="Feature slug to anchor the traversal.")
@click.option(
    "--error", default=None, help="Error signature slug (normalized_error) to narrow results."
)
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

    client = require_quine(config)

    packet = asyncio.run(_run_diagnose(project, feature, error, symptom, client))

    if as_json:
        click.echo(json.dumps(dataclasses.asdict(packet)))
    else:
        _print_packet(packet)


def _print_packet(packet: DebugPacket) -> None:
    click.echo(
        f"Feature: {packet.issue.anchors.features[0] if packet.issue.anchors.features else '(unknown)'}"
    )
    if packet.issue.anchors.errors:
        click.echo(f"Error:   {packet.issue.anchors.errors[0]}")
    if packet.issue.anchors.symptoms:
        click.echo(f"Symptom: {packet.issue.anchors.symptoms[0]}")
    click.echo("")

    if (
        not packet.known_issues
        and not packet.prior_fixes
        and not packet.relevant_files
        and not packet.relevant_tests
    ):
        click.echo("  (no results)")
        return

    if packet.known_issues:
        click.echo("Known Issues:")
        for ki in packet.known_issues:
            click.echo(f"  {ki.id}  {ki.summary}")

    if packet.prior_fixes:
        click.echo("Fixes:")
        for fix in packet.prior_fixes:
            commit = f" ({fix.commit})" if fix.commit else ""
            click.echo(f"  {fix.id}{commit}  {fix.summary}")

    if packet.relevant_files:
        click.echo("Files:")
        for f in packet.relevant_files:
            click.echo(f"  {f}")

    if packet.relevant_tests:
        click.echo("Tests:")
        for t in packet.relevant_tests:
            click.echo(f"  {t}")
