from __future__ import annotations

import re
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
    AffectedArea,
    DebugPacket,
    IssueAnchors,
    IssueSummary,
    KnownIssueRef,
    PriorFix,
    RecentCommit,
)

_KI_CAP = 10
_FIX_CAP = 10
_FILE_CAP = 20

_FILE_PATH_RE = re.compile(
    r'\b([\w.-]+/[\w./-]+\.(?:c|h|cpp|hpp|py|js|ts|md|sh|yaml|yml))\b'
)


def _is_test_path(path: str) -> bool:
    """Return True if path looks like a test file by convention."""
    parts = path.replace("\\", "/").split("/")
    filename = parts[-1] if parts else path
    return filename.startswith("test_") or "tests" in parts


def _pre_match_modules(text: str, module_source_files: dict[str, list[str]]) -> list[str]:
    """Return module slugs whose source files are explicitly mentioned in text."""
    mentioned = {m.group(1) for m in _FILE_PATH_RE.finditer(text)}
    if not mentioned:
        return []
    matched: list[str] = []
    for slug, files in module_source_files.items():
        if any(f in mentioned for f in files):
            matched.append(slug)
    return matched


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
    seen: dict[str, dict] = {}
    for file_path in file_paths:
        node_kind = "test-file" if _is_test_path(file_path) else "file"
        rows = await client.query(
            f"MATCH (f) WHERE id(f) = idFrom('{node_kind}', $project_slug, $file_path) "
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
) -> tuple[list[str], list[str], str]:
    """Return (source_paths, test_paths, resolved_as).

    resolved_as is 'feature' or 'module'. Source files come via
    Feature->IMPLEMENTED_BY->Module->DEFINED_IN->File; test files via
    Feature->HAS_TEST->File. Falls back to Module slug if Feature has no files.
    """
    rows = await client.query(
        "MATCH (f) WHERE id(f) = idFrom('feature', $project_slug, $feature_slug) "
        "OPTIONAL MATCH (f)-[:IMPLEMENTED_BY]->(m) "
        "OPTIONAL MATCH (m)-[:DEFINED_IN]->(file) "
        "RETURN f, m, file",
        {"project_slug": project_slug, "feature_slug": feature_slug},
    )
    source_paths = [
        row[2]["properties"]["repo_path"]
        for row in rows
        if len(row) > 2 and row[2] and isinstance(row[2], dict)
        and row[2].get("properties", {}).get("repo_path")
    ]

    test_rows = await client.query(
        "MATCH (f) WHERE id(f) = idFrom('feature', $project_slug, $feature_slug) "
        "OPTIONAL MATCH (f)-[:HAS_TEST]->(file) "
        "WHERE file.node_type = 'TestFile' "
        "RETURN file",
        {"project_slug": project_slug, "feature_slug": feature_slug},
    )
    test_paths = [
        row[0]["properties"]["repo_path"]
        for row in test_rows
        if row and row[0] and isinstance(row[0], dict)
        and row[0].get("properties", {}).get("repo_path")
    ]

    if source_paths or test_paths:
        return source_paths, test_paths, "feature"

    # Fallback: treat slug as a Module slug
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

    # Walk up to the parent Feature to get its HAS_TEST → TestFile edges.
    test_rows = await client.query(
        "MATCH (m) WHERE id(m) = idFrom('module', $project_slug, $feature_slug) "
        "OPTIONAL MATCH (f)-[:IMPLEMENTED_BY]->(m) "
        "OPTIONAL MATCH (f)-[:HAS_TEST]->(tfile) "
        "WHERE tfile.node_type = 'TestFile' "
        "RETURN tfile",
        {"project_slug": project_slug, "feature_slug": feature_slug},
    )
    module_test_paths = [
        row[0]["properties"]["repo_path"]
        for row in test_rows
        if row and row[0] and isinstance(row[0], dict)
        and row[0].get("properties", {}).get("repo_path")
    ]

    return module_paths, module_test_paths, "module"


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


async def _fetch_fix_commit_sha(
    fix_id: str,
    project_slug: str,
    client: QuineClient,
) -> str:
    """Return the short commit SHA for a fix via Fix-[:IMPLEMENTED_IN]->Commit, or ''."""
    rows = await client.query(
        "MATCH (f) WHERE id(f) = idFrom('fix', $project_slug, $fix_id) "
        "MATCH (f)-[:IMPLEMENTED_IN]->(c:Commit) "
        "RETURN c",
        {"project_slug": project_slug, "fix_id": fix_id},
    )
    for row in rows:
        if row and row[0] and isinstance(row[0], dict):
            sha = row[0].get("properties", {}).get("sha", "")
            if sha:
                return str(sha)[:7]
    return ""


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
    module_source_files: dict[str, list[str]] | None = None,
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
    mentioned_files: list[str] = []

    if not feature_slugs and not error_sigs:
        # @spec DRE-ANCH-004, DRE-ANCH-005, DRE-ANCH-006, DRE-ANCH-007
        if issue.raw_text is None:
            raise DREAnchorError(
                f"CustomerIssue id={issue_id} has no graph anchors and no raw_text"
            )

        pre_matched = _pre_match_modules(issue.raw_text, module_source_files or {})

        try:
            parse_result = await gateway.parse_ticket(
                issue.raw_text, project_slug, backend=backend,
                valid_slugs=valid_slugs, feature_slugs=feature_slugs, module_slugs=module_slugs,
                feature_descriptions=feature_descriptions, module_descriptions=module_descriptions,
                module_elements=module_elements, module_source_files=module_source_files,
            )
        except LLMResponseError as exc:
            raise DREAnchorError(f"LLM anchor extraction failed: {exc}") from exc
        except LLMUnavailableError as exc:
            raise DRELLMUnavailableError(f"LLM gateway unreachable: {exc}") from exc

        merged: list[str] = list(pre_matched)
        for llm_slug in parse_result.feature_slugs:
            if llm_slug not in merged:
                merged.append(llm_slug)
        feature_slugs = merged

        error_sigs = list(parse_result.error_signatures)
        symptoms = list(parse_result.symptoms)
        mentioned_files = list(parse_result.mentioned_files)

    anchor_count = len(feature_slugs) + len(error_sigs)

    # Accumulators
    ki_counts: dict[str, int] = {}
    ki_meta: dict[str, dict[str, str]] = {}
    fix_counts: dict[str, int] = {}
    fix_meta: dict[str, dict[str, str]] = {}
    file_counts: dict[str, int] = {}       # source files
    test_file_counts: dict[str, int] = {}  # test files
    matched_anchors = 0
    resolved_module_slugs: list[str] = []
    resolved_feature_slugs: list[str] = []

    # @spec DRE-TRAV-001
    for slug in feature_slugs:
        try:
            src_paths, tst_paths, resolved_as = await _traverse_feature_to_files(
                slug, project_slug, client
            )
        except Exception as exc:
            raise DREGraphUnavailableError(f"Quine unreachable during traversal: {exc}") from exc

        if src_paths or tst_paths:
            matched_anchors += 1
            for path in src_paths:
                _accumulate_match_count(file_counts, path, 1)
            for path in tst_paths:
                _accumulate_match_count(test_file_counts, path, 1)
            if resolved_as == "module":
                resolved_module_slugs.append(slug)
            else:
                resolved_feature_slugs.append(slug)

    # @spec DRE-TRAV-002, DRE-TRAV-003
    for err in error_sigs:
        try:
            ki_props_list = await _traverse_error_to_known_issues(err, project_slug, client)
        except Exception as exc:
            raise DREGraphUnavailableError(f"Quine unreachable during traversal: {exc}") from exc
        if ki_props_list:
            matched_anchors += 1
        for ki_node_id, props in ki_props_list:
            ki_id = props["issue_id"]
            _accumulate_match_count(ki_counts, ki_id, 1)
            ki_meta[ki_id] = props

            # @spec DRE-TRAV-003, DRE-SCORE-006
            try:
                fix_props_list = await _traverse_ki_to_fixes(ki_node_id, project_slug, client)
            except Exception as exc:
                raise DREGraphUnavailableError(f"Quine unreachable during traversal: {exc}") from exc
            for fix_props in fix_props_list:
                fid = fix_props["fix_id"]
                _accumulate_match_count(fix_counts, fid, 1)
                fix_meta[fid] = fix_props

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

    # Seed explicitly mentioned files from LLM parse into the right bucket
    for fpath in mentioned_files:
        if _is_test_path(fpath):
            if fpath not in test_file_counts:
                test_file_counts[fpath] = 1
        else:
            if fpath not in file_counts:
                file_counts[fpath] = 1

    # Sort and cap
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
    test_file_items = _sort_and_cap(
        [{"id": k, "match_count": v} for k, v in test_file_counts.items()], _FILE_CAP
    )

    relevant_files = [item["id"] for item in file_items]
    relevant_tests = [item["id"] for item in test_file_items]

    known_issues = [
        KnownIssueRef(
            id=item["id"],
            summary=ki_meta[item["id"]].get("summary", ""),
        )
        for item in ki_items
    ]

    # Fetch commit SHAs for fixes (best-effort; empty string when not available)
    prior_fixes: list[PriorFix] = []
    for item in fix_items:
        fid = item["id"]
        try:
            commit_sha = await _fetch_fix_commit_sha(fid, project_slug, client)
        except Exception:
            commit_sha = ""
        prior_fixes.append(PriorFix(
            id=fid,
            commit=commit_sha,
            summary=fix_meta[fid].get("summary", ""),
        ))

    affected_areas: list[AffectedArea] = []
    for slug in resolved_feature_slugs:
        affected_areas.append(AffectedArea(type="feature", id=f"feature:{slug}", name=slug))
    for slug in resolved_module_slugs:
        affected_areas.append(AffectedArea(type="module", id=f"module:{slug}", name=slug))

    all_file_paths = relevant_files + relevant_tests
    raw_commits = await _traverse_files_to_recent_commits(all_file_paths, project_slug, client)
    raw_text = issue.raw_text or issue.summary
    try:
        summary = await gateway.summarise_packet(
            issue_text=raw_text,
            module_slugs=[a.name for a in affected_areas if a.type == "module"],
            error_signatures=error_sigs,
            symptoms=symptoms,
            relevant_files=relevant_files,
            relevant_tests=relevant_tests,
            recent_commits=[
                {"timestamp": c.get("timestamp", ""), "author_name": c.get("author_name", ""), "message": c.get("message", "")}
                for c in raw_commits
            ],
            known_issues=[ki.summary for ki in known_issues],
            backend=backend,
        )
    except Exception:
        summary = issue.summary

    return DebugPacket(
        issue=IssueSummary(
            summary=issue.summary,
            anchors=IssueAnchors(
                features=resolved_feature_slugs,
                errors=error_sigs,
                symptoms=symptoms,
            ),
        ),
        affected_areas=affected_areas,
        relevant_files=relevant_files,
        relevant_tests=relevant_tests,
        known_issues=known_issues,
        prior_fixes=prior_fixes,
        recent_commits=[
            RecentCommit(
                sha=c.get("sha", ""),
                timestamp=c.get("timestamp", ""),
                author_name=c.get("author_name", ""),
                message=c.get("message", ""),
                files_touched=c.get("files_touched", []),
            )
            for c in raw_commits
        ],
        summary=summary,
    )
