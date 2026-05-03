from __future__ import annotations

from typing import Any

from modok.llm import gateway
from modok.llm.errors import LLMResponseError, LLMUnavailableError
from modok.quine.client import QuineClient
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
    CommitRef,
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
    issue_id: str,
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

async def _traverse_files_to_recent_commits(
    file_paths: list[str],
    project_slug: str,
    client: QuineClient,
    limit: int = 10,
) -> list[dict]:
    """Return up to `limit` most recent commits touching any of the given files."""
    if not file_paths:
        return []
    # sha → commit props dict; we accumulate files_touched across per-file queries
    seen: dict[str, dict] = {}
    for file_path in file_paths:
        rows = await client.query(
            "MATCH (f) WHERE id(f) = idFrom('file', $project_slug, $file_path) "
            "OPTIONAL MATCH (c)-[:TOUCHES]->(f) "
            "RETURN f, c",
            {"project_slug": project_slug, "file_path": file_path},
        )
        for row in rows:
            if len(row) < 2 or not row[1] or not isinstance(row[1], dict):
                continue
            props = row[1].get("properties", {})
            sha = props.get("sha")
            if not sha or props.get("project_slug") != project_slug:
                continue
            if sha not in seen:
                seen[sha] = dict(props)
                seen[sha]["files_touched"] = []
            if file_path not in seen[sha]["files_touched"]:
                seen[sha]["files_touched"].append(file_path)
    all_commits = list(seen.values())
    all_commits.sort(key=lambda c: c.get("timestamp", ""), reverse=True)
    return all_commits[:limit]


async def _traverse_feature_to_files(
    feature_slug: str,
    project_slug: str,
    client: QuineClient,
) -> tuple[list[str], str]:
    """Return (file_paths, resolved_as) where resolved_as is 'feature' or 'module'.

    Tries the slug as a Feature first. If no results, tries it as a Module slug
    so that module-level anchors (e.g. 'lighthouse-ble') still resolve to files.
    """
    rows = await client.query(
        "MATCH (f) WHERE id(f) = idFrom('feature', $project_slug, $feature_slug) "
        "OPTIONAL MATCH (f)-[:IMPLEMENTED_BY]->(m) "
        "OPTIONAL MATCH (m)-[:DEFINED_IN]->(file) "
        "RETURN f, m, file",
        {"project_slug": project_slug, "feature_slug": feature_slug},
    )
    paths = [
        row[2]["properties"]["repo_path"]
        for row in rows
        if len(row) > 2 and row[2] and isinstance(row[2], dict)
        and row[2].get("properties", {}).get("repo_path")
    ]
    if paths:
        return paths, "feature"
    # Fallback: treat slug as a Module slug (same query pattern as modok recall)
    rows = await client.query(
        "MATCH (m) WHERE id(m) = idFrom('module', $project_slug, $feature_slug) "
        "OPTIONAL MATCH (m)-[:DEFINED_IN]->(file) "
        "RETURN m, file",
        {"project_slug": project_slug, "feature_slug": feature_slug},
    )
    module_paths = [
        row[1]["properties"]["repo_path"]
        for row in rows
        if len(row) > 1 and row[1] and isinstance(row[1], dict)
        and row[1].get("properties", {}).get("repo_path")
    ]
    return module_paths, "module"


async def _traverse_error_to_known_issues(
    normalized_error: str,
    project_slug: str,
    client: QuineClient,
) -> list[tuple[str, dict[str, str]]]:
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
    ki_node_id: str,
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
    issue_id: str,
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
    issue_id: str,
    project_slug: str,
    client: QuineClient,
    backend: str = "local",
    valid_slugs: list[str] | None = None,
    feature_slugs: list[str] | None = None,
    module_slugs: list[str] | None = None,
    feature_descriptions: dict[str, str] | None = None,
    module_descriptions: dict[str, str] | None = None,
    module_elements: dict[str, list[str]] | None = None,
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
            parse_result = await gateway.parse_ticket(
                issue.raw_text, project_slug, backend=backend,
                valid_slugs=valid_slugs, feature_slugs=feature_slugs, module_slugs=module_slugs,
                feature_descriptions=feature_descriptions, module_descriptions=module_descriptions,
                module_elements=module_elements,
            )
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

    # Accumulators: keyed by logical ID string
    ki_counts: dict[str, int] = {}           # known_issue_id → match_count
    ki_meta: dict[str, dict[str, str]] = {}  # known_issue_id → props
    fix_counts: dict[str, int] = {}          # fix_id → match_count
    fix_meta: dict[str, dict[str, str]] = {} # fix_id → props
    file_counts: dict[str, int] = {}         # repo_path → match_count
    evidence: list[EvidenceAnchor] = []
    resolved_module_slugs: list[str] = []    # slugs that resolved via Module fallback

    # @spec DRE-TRAV-001
    for slug in feature_slugs:
        try:
            paths, resolved_as = await _traverse_feature_to_files(slug, project_slug, client)
        except Exception as exc:
            raise DREGraphUnavailableError(f"Quine unreachable during traversal: {exc}") from exc
        matched_ids = []
        for path in paths:
            _accumulate_match_count(file_counts, path, 1)
            matched_ids.append(path)
        if matched_ids:
            if resolved_as == "module":
                resolved_module_slugs.append(slug)
            evidence.append(EvidenceAnchor(
                anchor_type=resolved_as,
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

    # Build AnchorSet: reclassify slugs that resolved via Module fallback
    resolved_feature_slugs = [s for s in feature_slugs if s not in resolved_module_slugs]
    anchors = AnchorSet(
        feature_slugs=resolved_feature_slugs,
        module_slugs=resolved_module_slugs,
        error_signatures=error_sigs,
        symptoms=symptoms,
    )

    # Compute confidence
    # @spec DRE-CONF-001
    matched_anchors = 0
    for slug in feature_slugs:
        # slug may have resolved as "feature" or "module" — either counts as matched
        if any(ev.anchor_value == slug and ev.anchor_type in ("feature", "module") for ev in evidence):
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
            pr_url=fix_meta[item["id"]].get("pr_url") or None,
        )
        for item in fix_items
    ]
    relevant_files = [
        FileRef(repo_path=item["id"], match_count=item["match_count"])
        for item in file_items
    ]

    file_paths = [f.repo_path for f in relevant_files]
    raw_commits = await _traverse_files_to_recent_commits(file_paths, project_slug, client)
    recent_commits = [
        CommitRef(
            sha=c.get("sha", ""),
            message=c.get("message", ""),
            author_name=c.get("author_name", ""),
            timestamp=c.get("timestamp", ""),
            files_touched=c.get("files_touched", []),
        )
        for c in raw_commits
    ]

    raw_text = issue.raw_text or issue.summary
    try:
        generated_summary = await gateway.summarise_packet(
            issue_text=raw_text,
            module_slugs=anchors.module_slugs,
            error_signatures=anchors.error_signatures,
            symptoms=anchors.symptoms,
            relevant_files=[f.repo_path for f in relevant_files],
            recent_commits=[
                {"timestamp": c.timestamp, "author_name": c.author_name, "message": c.message}
                for c in recent_commits
            ],
            known_issues=[ki.summary for ki in known_issues],
            backend=backend,
        )
    except Exception:
        generated_summary = ""

    return DebugPacket(
        issue_summary=issue.summary,
        anchors=anchors,
        anchor_count=anchor_count,
        known_issues=known_issues,
        recent_fixes=recent_fixes,
        relevant_files=relevant_files,
        recent_commits=recent_commits,
        evidence=evidence,
        confidence=confidence,
        summary=generated_summary,
    )
