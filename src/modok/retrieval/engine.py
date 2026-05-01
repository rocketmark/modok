from __future__ import annotations

from typing import Any

from modok.llm import gateway
from modok.llm.errors import LLMResponseError, LLMUnavailableError
from modok.quine.client import QuineClient, QuineNodeId
from modok.quine.errors import QuineNodeNotFoundError
from modok.quine.models import CustomerIssue
from modok.retrieval.errors import (
    DREAnchorError,
    DREGraphUnavailableError,
    DRELLMUnavailableError,
    DRENotFoundError,
)
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


# ---------------------------------------------------------------------------
# Pure helpers (tested as properties)
# ---------------------------------------------------------------------------

def _sort_and_cap(items: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    return sorted(items, key=lambda x: x["match_count"], reverse=True)[:cap]


def _compute_confidence(matched: int, total: int) -> float:
    if total == 0:
        return 0.0
    return float(matched) / float(total)


def _accumulate_match_count(counts: dict[str, int], key: str, delta: int) -> None:
    counts[key] = counts.get(key, 0) + delta


# ---------------------------------------------------------------------------
# Anchor extraction
# ---------------------------------------------------------------------------

async def _graph_anchors(
    issue_id: QuineNodeId,
    project_slug: str,
    client: QuineClient,
) -> tuple[list[str], list[str]]:
    """Return (feature_slugs, error_signatures) from graph edges, project-scoped."""
    feature_rows = await client.query(
        "MATCH (ci:CustomerIssue) WHERE id(ci) = $issue_id "
        "MATCH (ci)-[:AFFECTS]->(f:Feature {project_slug: $project_slug}) "
        "RETURN f.feature_slug",
        {"issue_id": issue_id, "project_slug": project_slug},
    )
    error_rows = await client.query(
        "MATCH (ci:CustomerIssue) WHERE id(ci) = $issue_id "
        "MATCH (ci)-[:HAS_ERROR]->(e:ErrorSignature {project_slug: $project_slug}) "
        "RETURN e.normalized_error",
        {"issue_id": issue_id, "project_slug": project_slug},
    )
    feature_slugs = [
        row[0]["properties"]["feature_slug"]
        for row in feature_rows
        if row and row[0].get("properties", {}).get("feature_slug")
    ]
    error_sigs = [
        row[0]["properties"]["normalized_error"]
        for row in error_rows
        if row and row[0].get("properties", {}).get("normalized_error")
    ]
    return feature_slugs, error_sigs


# ---------------------------------------------------------------------------
# Graph traversals
# ---------------------------------------------------------------------------

async def _traverse_feature_to_files(
    feature_slug: str,
    project_slug: str,
    client: QuineClient,
) -> list[str]:
    rows = await client.query(
        "MATCH (f:Feature {project_slug: $project_slug, feature_slug: $feature_slug}) "
        "MATCH (f)-[:IMPLEMENTED_BY]->(m:Module)-[:DEFINED_IN]->(file:File) "
        "RETURN file",
        {"project_slug": project_slug, "feature_slug": feature_slug},
    )
    return [
        row[0]["properties"]["repo_path"]
        for row in rows
        if row and row[0].get("properties", {}).get("repo_path")
    ]


async def _traverse_error_to_known_issues(
    normalized_error: str,
    project_slug: str,
    client: QuineClient,
) -> list[tuple[QuineNodeId, dict[str, str]]]:
    """Return (quine_node_id, props) for each KnownIssue reachable from this error."""
    rows = await client.query(
        "MATCH (e:ErrorSignature {project_slug: $project_slug, normalized_error: $normalized_error}) "
        "MATCH (e)<-[:HAS_ERROR]-(ki:KnownIssue) "
        "RETURN ki",
        {"project_slug": project_slug, "normalized_error": normalized_error},
    )
    return [
        (row[0]["id"], row[0]["properties"])
        for row in rows
        if row and row[0].get("properties", {}).get("issue_id")
    ]


# @spec DRE-TRAV-005
async def _traverse_ki_to_fixes(
    ki_node_id: QuineNodeId,
    project_slug: str,
    client: QuineClient,
) -> list[dict[str, str]]:
    rows = await client.query(
        "MATCH (ki:KnownIssue) WHERE id(ki) = $ki_node_id "
        "MATCH (ki)-[:RESOLVED_BY]->(fix:Fix {project_slug: $project_slug}) "
        "RETURN fix",
        {"project_slug": project_slug, "ki_node_id": ki_node_id},
    )
    return [
        row[0]["properties"]
        for row in rows
        if row and row[0].get("properties", {}).get("fix_id")
    ]


# @spec DRE-TRAV-005
async def _traverse_similarity(
    issue_id: QuineNodeId,
    project_slug: str,
    client: QuineClient,
) -> list[tuple[dict[str, str], str]]:
    """Return list of (ki_properties, review_status) for non-rejected similarity matches."""
    rows = await client.query(
        "MATCH (ci:CustomerIssue) WHERE id(ci) = $issue_id "
        "MATCH (ci)-[:HAS_SIMILARITY_MATCH]->(sm:SimilarityMatch)-[:MATCHES]->(ki:KnownIssue {project_slug: $project_slug}) "
        "WHERE sm.review_status IN ['candidate', 'confirmed'] "
        "RETURN ki, sm.review_status",
        {"issue_id": issue_id, "project_slug": project_slug},
    )
    results = []
    for row in rows:
        if not row or len(row) < 2:
            continue
        ki_props = row[0].get("properties", {})
        review_status = row[1].get("review_status", "candidate") if isinstance(row[1], dict) else str(row[1])
        if ki_props.get("issue_id"):
            results.append((ki_props, review_status))
    return results


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

# @spec DRE-IFACE-001, DRE-IFACE-002, DRE-IFACE-003
async def retrieve(
    issue_id: QuineNodeId,
    project_slug: str,
    client: QuineClient,
    backend: str = "local",
) -> DebugPacket:
    # Fetch and validate the CustomerIssue node
    try:
        issue = await client.get_node(issue_id, CustomerIssue)
    except QuineNodeNotFoundError as exc:
        raise DRENotFoundError(f"CustomerIssue id={issue_id} not found") from exc
    except (ConnectionError, OSError, TimeoutError) as exc:
        raise DREGraphUnavailableError(f"Quine unreachable: {exc}") from exc
    except Exception as exc:
        raise DRENotFoundError(f"CustomerIssue id={issue_id} not found: {exc}") from exc

    # @spec DRE-IFACE-001, DRE-ERR-002
    if issue.project_slug != project_slug:
        raise DRENotFoundError(
            f"CustomerIssue id={issue_id} belongs to project '{issue.project_slug}', "
            f"not '{project_slug}'"
        )

    # Anchor extraction — graph-first
    # @spec DRE-ANCH-001, DRE-ANCH-002, DRE-ANCH-003
    try:
        feature_slugs, error_sigs = await _graph_anchors(issue_id, project_slug, client)
    except Exception as exc:
        raise DREGraphUnavailableError(f"Quine unreachable during anchor extraction: {exc}") from exc

    symptoms: list[str] = []

    if not feature_slugs and not error_sigs:
        # @spec DRE-ANCH-004, DRE-ANCH-005, DRE-ANCH-006, DRE-ANCH-007
        if issue.raw_text is None:
            raise DREAnchorError(
                f"CustomerIssue id={issue_id} has no graph anchors and no raw_text"
            )
        try:
            parse_result = await gateway.parse_ticket(issue.raw_text, project_slug, backend=backend)
        except LLMResponseError as exc:
            raise DREAnchorError(f"LLM anchor extraction failed: {exc}") from exc
        except LLMUnavailableError as exc:
            raise DRELLMUnavailableError(f"LLM gateway unreachable: {exc}") from exc

        feature_slugs = [parse_result.feature_slug] if parse_result.feature_slug else []
        error_sigs = list(parse_result.error_signatures)
        symptoms = list(parse_result.symptoms)

        # @spec DRE-CONF-002: zero anchor instances after extraction → error
        if not feature_slugs and not error_sigs:
            raise DREAnchorError(
                f"CustomerIssue id={issue_id}: LLM returned no anchor instances"
            )

    anchor_count = len(feature_slugs) + len(error_sigs)
    anchors = AnchorSet(
        feature_slugs=feature_slugs,
        error_signatures=error_sigs,
        symptoms=symptoms,
    )

    # Accumulators: keyed by logical ID string
    ki_counts: dict[str, int] = {}           # known_issue_id → match_count
    ki_meta: dict[str, dict[str, str]] = {}  # known_issue_id → props
    fix_counts: dict[str, int] = {}          # fix_id → match_count
    fix_meta: dict[str, dict[str, str]] = {} # fix_id → props
    file_counts: dict[str, int] = {}         # repo_path → match_count
    evidence: list[EvidenceAnchor] = []

    # @spec DRE-TRAV-001
    for slug in feature_slugs:
        try:
            paths = await _traverse_feature_to_files(slug, project_slug, client)
        except Exception as exc:
            raise DREGraphUnavailableError(f"Quine unreachable during traversal: {exc}") from exc
        matched_ids = []
        for path in paths:
            _accumulate_match_count(file_counts, path, 1)
            matched_ids.append(path)
        if matched_ids:
            evidence.append(EvidenceAnchor(
                anchor_type="feature",
                anchor_value=slug,
                matched_node_ids=matched_ids,
            ))

    # @spec DRE-TRAV-002, DRE-TRAV-003
    for err in error_sigs:
        try:
            ki_props_list = await _traverse_error_to_known_issues(err, project_slug, client)
        except Exception as exc:
            raise DREGraphUnavailableError(f"Quine unreachable during traversal: {exc}") from exc
        matched_ids = []
        for ki_node_id, props in ki_props_list:
            ki_id = props["issue_id"]
            _accumulate_match_count(ki_counts, ki_id, 1)
            ki_meta[ki_id] = props
            matched_ids.append(ki_id)

            # @spec DRE-TRAV-003, DRE-SCORE-006
            try:
                fix_props_list = await _traverse_ki_to_fixes(
                    ki_node_id, project_slug, client
                )
            except Exception as exc:
                raise DREGraphUnavailableError(f"Quine unreachable during traversal: {exc}") from exc
            for fix_props in fix_props_list:
                fid = fix_props["fix_id"]
                _accumulate_match_count(fix_counts, fid, 1)
                fix_meta[fid] = fix_props

        if matched_ids:
            evidence.append(EvidenceAnchor(
                anchor_type="error_signature",
                anchor_value=err,
                matched_node_ids=matched_ids,
            ))

    # @spec DRE-TRAV-004, DRE-SCORE-002
    try:
        sim_results = await _traverse_similarity(issue_id, project_slug, client)
    except Exception as exc:
        raise DREGraphUnavailableError(f"Quine unreachable during similarity traversal: {exc}") from exc
    for props, review_status in sim_results:
        ki_id = props["issue_id"]
        weight = 2 if review_status == "confirmed" else 1
        _accumulate_match_count(ki_counts, ki_id, weight)
        ki_meta.setdefault(ki_id, props)

    # Compute confidence
    # @spec DRE-CONF-001
    matched_anchors = 0
    for slug in feature_slugs:
        if any(ev.anchor_type == "feature" and ev.anchor_value == slug for ev in evidence):
            matched_anchors += 1
    for err in error_sigs:
        if any(ev.anchor_type == "error_signature" and ev.anchor_value == err for ev in evidence):
            matched_anchors += 1
    confidence = _compute_confidence(matched_anchors, anchor_count)

    # Sort and cap all result lists
    # @spec DRE-SCORE-003, DRE-SCORE-004
    ki_items = _sort_and_cap(
        [{"id": k, "match_count": v} for k, v in ki_counts.items()], _KI_CAP
    )
    fix_items = _sort_and_cap(
        [{"id": k, "match_count": v} for k, v in fix_counts.items()], _FIX_CAP
    )
    file_items = _sort_and_cap(
        [{"id": k, "match_count": v} for k, v in file_counts.items()], _FILE_CAP
    )

    known_issues = [
        KnownIssueRef(
            known_issue_id=item["id"],
            summary=ki_meta[item["id"]].get("summary", ""),
            status=ki_meta[item["id"]].get("status", ""),
            match_count=item["match_count"],
        )
        for item in ki_items
    ]
    recent_fixes = [
        FixRef(
            fix_id=item["id"],
            summary=fix_meta[item["id"]].get("summary", ""),
            kind=fix_meta[item["id"]].get("kind", ""),
            match_count=item["match_count"],
        )
        for item in fix_items
    ]
    relevant_files = [
        FileRef(repo_path=item["id"], match_count=item["match_count"])
        for item in file_items
    ]

    return DebugPacket(
        issue_summary=issue.summary,
        anchors=anchors,
        anchor_count=anchor_count,
        known_issues=known_issues,
        recent_fixes=recent_fixes,
        relevant_files=relevant_files,
        evidence=evidence,
        confidence=confidence,
    )
