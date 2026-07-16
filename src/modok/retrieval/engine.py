from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

from modok.llm import gateway
from modok.llm.errors import LLMGatewayError, LLMUnavailableError
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
    CoveredTest,
    DebugPacket,
    EvidenceItem,
    IssueAnchors,
    IssueSummary,
    KnownIssueRef,
    PriorFix,
    RecentCommit,
    RecentDependencyChange,
    RecentTestFailure,
    ScoredCandidate,
)
from modok.text_utils import extract_text_tokens
from modok.text_utils import tokenize as _tokenize

_KI_CAP = 10
_FIX_CAP = 10
_FILE_CAP = 20

_FILE_PATH_RE = re.compile(r"\b([\w.-]+/[\w./-]+\.(?:c|h|cpp|hpp|py|js|ts|md|sh|yaml|yml))\b")


def _is_test_path(path: str) -> bool:
    """Return True if path looks like a test file by convention."""
    parts = path.replace("\\", "/").split("/")
    filename = parts[-1] if parts else path
    return filename.startswith("test_") or "tests" in parts


_SOURCE_EXTS = {
    ".py",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".rs",
    ".go",
    ".java",
    ".rb",
    ".swift",
    ".sh",
}


def _is_source_path(path: str) -> bool:
    if os.path.splitext(path)[1].lower() in _SOURCE_EXTS:
        return True
    # Extensionless executable scripts under scripts/ are real operational
    # (deployment/provisioning) code, not documentation — found live: a
    # directly-relevant script with no file extension was penalized with
    # the same 0.25x actionability multiplier as a markdown doc.
    parts = path.replace("\\", "/").split("/")
    if len(parts) > 1 and "scripts" in parts[:-1] and not os.path.splitext(path)[1]:
        return True
    return False


def _path_actionability_multiplier(path: str) -> float:
    """Penalize non-source files (docs, specs, markdown) that can't contain bugs."""
    if _is_source_path(path) or _is_test_path(path):
        return 1.0
    return 0.25


# @spec DRE-ANCH-009
def _pre_match_modules(
    text: str,
    module_source_files: dict[str, list[str]],
    module_elements: dict[str, list[str]] | None = None,
) -> list[str]:
    """Return module slugs whose source files are mentioned in text or whose
    element names have token overlap with words extracted from text."""
    matched: list[str] = []
    seen: set[str] = set()

    # File-path matching
    mentioned_files = {m.group(1) for m in _FILE_PATH_RE.finditer(text)}
    for slug, files in module_source_files.items():
        if any(f in mentioned_files for f in files):
            if slug not in seen:
                seen.add(slug)
                matched.append(slug)

    # Element-token matching: tokenize words from ticket text, check element subsets
    if module_elements:
        text_tokens: set[str] = extract_text_tokens(text)
        for slug, elements in module_elements.items():
            if slug in seen:
                continue
            for elem in elements:
                elem_tokens = _tokenize(elem)
                if elem_tokens and elem_tokens.issubset(text_tokens):
                    seen.add(slug)
                    matched.append(slug)
                    break

    return matched


# ---------------------------------------------------------------------------
# Function-anchor matching helpers
# ---------------------------------------------------------------------------

# @spec DRE-TOKEN-002, DRE-TOKEN-003
def _build_anchor_tokens(
    feature_slugs: list[str],
    error_sigs: list[str],
    symptoms: list[str],
) -> set[str]:
    tokens: set[str] = set()
    for term in feature_slugs + error_sigs + symptoms:
        tokens.update(_tokenize(term))
    return tokens


# @spec DRE-FUNC-001
def _matching_defs(hunk_data: list[dict], anchor_tokens: set[str]) -> list[str]:
    """Return def names from hunk_data whose tokens overlap with anchor_tokens."""
    matched: list[str] = []
    for hunk in hunk_data:
        for def_name in hunk.get("defs", []):
            if _tokenize(def_name) & anchor_tokens:
                matched.append(def_name)
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


def _add_evidence(
    evidence_map: dict[str, list[EvidenceItem]],
    path: str,
    item: EvidenceItem,
) -> None:
    if path not in evidence_map:
        evidence_map[path] = []
    evidence_map[path].append(item)


# @spec DRE-CAND-001, DRE-CAND-002, DRE-CAND-006
_NON_CORROBORATING_TYPES = {"recent_commit", "feature_anchor"}


def _score_candidate(items: list[EvidenceItem]) -> float:
    by_type: dict[str, list[float]] = {}
    penalties = 0.0
    for item in items:
        if item.score < 0:
            penalties += item.score
        else:
            by_type.setdefault(item.type, []).append(item.score)
    total = 0.0
    for scores in by_type.values():
        scores.sort(reverse=True)
        total += sum(s * (0.5**i) for i, s in enumerate(scores))
    # A non-corroborating type (weak/broad, e.g. bare recency or a peripheral
    # feature match) only earns diversity-bonus credit when the candidate
    # also has at least one genuinely direct evidence type to reinforce —
    # otherwise it's manufacturing apparent strength from weak signals alone.
    has_direct_evidence = bool(by_type.keys() - _NON_CORROBORATING_TYPES)
    diversity_type_count = len(by_type) if has_direct_evidence else 0
    total += 3.0 * min(max(diversity_type_count - 1, 0), 4)
    total += penalties
    return round(total, 1)


# @spec DRE-CAND-003
def _confidence_label(score: float) -> str:
    if score >= 20.0:
        return "high"
    if score >= 10.0:
        return "medium"
    return "low"


# @spec DRE-CAND-004
def _build_scored_candidates(
    evidence_map: dict[str, list[EvidenceItem]],
    kind: str,
    cap: int,
) -> list[ScoredCandidate]:
    candidates = []
    for path, items in evidence_map.items():
        multiplier = _path_actionability_multiplier(path)
        all_items = list(items)
        if multiplier < 1.0:
            raw = _score_candidate(items)
            penalty_score = round(raw * (multiplier - 1.0), 1)
            all_items.append(
                EvidenceItem(
                    type="doc_penalty",
                    score=penalty_score,
                    explanation=f"Non-source file (×{multiplier} actionability penalty)",
                )
            )
        s = _score_candidate(all_items)
        candidates.append(
            ScoredCandidate(
                path=path,
                kind=kind,
                score=s,
                confidence=_confidence_label(s),
                evidence=all_items,
            )
        )
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:cap]


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
        "MATCH (ci) WHERE id(ci) = $issue_id AND ci.node_type = 'CustomerIssue' "
        "MATCH (ci)-[:AFFECTS]->(f) WHERE f.node_type = 'Feature' AND f.project_slug = $project_slug "
        "RETURN f.feature_slug",
        {"issue_id": issue_id, "project_slug": project_slug},
    )
    error_rows = await client.query(
        "MATCH (ci) WHERE id(ci) = $issue_id AND ci.node_type = 'CustomerIssue' "
        "MATCH (ci)-[:HAS_ERROR]->(e) WHERE e.node_type = 'ErrorSignature' AND e.project_slug = $project_slug "
        "RETURN e.normalized_error",
        {"issue_id": issue_id, "project_slug": project_slug},
    )
    # RETURN f.feature_slug / e.normalized_error project a scalar property,
    # not a node — real Quine returns the raw value directly as row[0], not
    # wrapped in a {"properties": {...}} node dict (found live: the old code
    # here assumed the node-dict shape and silently extracted nothing).
    feature_slugs = [row[0] for row in feature_rows if row and row[0]]
    error_sigs = [row[0] for row in error_rows if row and row[0]]
    return feature_slugs, error_sigs


_QUICK_SUMMARY_FILE_CAP = 5


# @spec DRE-QUICK-001, DRE-QUICK-002, DRE-QUICK-003
async def quick_investigation_summary(
    issue_id: str,
    project_slug: str,
    client: QuineClient,
    feature_source_files: dict[str, list[str]] | None = None,
) -> str:
    """Instant, mechanical summary for the immediate "investigation
    triggered" notification, posted before the full retrieve() pipeline runs.

    No LLM call — deliberately. An LLM call, even a small one, costs tens of
    seconds to a few minutes on local inference (found live: this function
    originally reused gateway.summarise_packet with a reduced input, which
    measured ~85s standalone; in production the gap to the second, full
    "results" comment was often just a few seconds, because retrieve()'s own
    summary call landed on an already-warm model — the intended head start
    mostly didn't materialize, since the slow part was the LLM call itself,
    not the traversal this function was built to skip). This function uses
    only graph-first anchors (already written at ingestion time, two fast
    Quine queries) and the registry's declared primary files per feature —
    pure data lookups, no generation — so it returns in about the time of a
    couple of Quine round-trips, not an LLM call.

    Never raises: any failure degrades to the CustomerIssue's own summary
    field (the ticket title), or "" if the node itself can't be fetched.
    """
    try:
        issue = await client.get_node(issue_id, CustomerIssue)
    except Exception:
        return ""

    fallback = issue.summary or ""
    try:
        feature_slugs, error_sigs = await _graph_anchors(issue_id, project_slug, client)
    except Exception:
        return fallback

    if not feature_slugs and not error_sigs:
        return fallback

    relevant_files: list[str] = []
    if feature_source_files:
        for slug in feature_slugs:
            for fpath in feature_source_files.get(slug, []):
                if fpath not in relevant_files:
                    relevant_files.append(fpath)

    parts = []
    if feature_slugs:
        parts.append(f"Features: {', '.join(feature_slugs)}")
    if error_sigs:
        parts.append(f"Errors: {', '.join(error_sigs)}")
    summary = " · ".join(parts)
    if relevant_files:
        summary += f". Likely files: {', '.join(relevant_files[:_QUICK_SUMMARY_FILE_CAP])}"
    return summary


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
            "OPTIONAL MATCH (c)-[e:TOUCHES]->(f) "
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
                seen[sha]["file_hunk_data"] = {}
                # Parse file_hunks stored on the Commit node
                raw = props.get("file_hunks", "")
                if raw:
                    try:
                        seen[sha]["file_hunk_data"] = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        pass
            if file_path not in seen[sha]["files_touched"]:
                seen[sha]["files_touched"].append(file_path)
    all_commits = list(seen.values())
    all_commits.sort(key=lambda c: c.get("timestamp", ""), reverse=True)
    return all_commits[:limit]


# @spec DEPG-DRE-003
async def _traverse_files_to_recent_dependency_changes(
    file_paths: list[str],
    project_slug: str,
    client: QuineClient,
) -> list[dict]:
    """Return dependency changes reachable from the given (already-anchored)
    file paths via USES_DEPENDENCY -> CHANGED_PACKAGE. Deduplicated by
    DependencyChange id. Deliberately not sorted or capped by recency, unlike
    _traverse_files_to_recent_commits — a candidate only ever reaches this
    traversal by already being an anchored file, so "recency" isn't the
    relevance signal here (docs/llds/dependency-graph-ingestion.md
    § Existing Retrieval Integration)."""
    seen: dict[Any, dict] = {}
    for file_path in file_paths:
        node_kind = "test-file" if _is_test_path(file_path) else "file"
        rows = await client.query(
            f"MATCH (f) WHERE id(f) = idFrom('{node_kind}', $project_slug, $file_path) "
            "MATCH (f)-[:USES_DEPENDENCY]->(pkg) "
            "MATCH (pkg)<-[:CHANGED_PACKAGE]-(dc) "
            "MATCH (dc)-[:TO_VERSION]->(tv) "
            "OPTIONAL MATCH (dc)-[:FROM_VERSION]->(fv) "
            "OPTIONAL MATCH (dc)-[:INTRODUCED_BY]->(c) "
            "OPTIONAL MATCH (dc)-[:MERGED_VIA]->(fix) "
            "RETURN dc, pkg, fv, tv, c, fix",
            {"project_slug": project_slug, "file_path": file_path},
        )
        for row in rows:
            if not row or not row[0] or not isinstance(row[0], dict):
                continue
            change_id = row[0].get("id")
            if change_id in seen:
                if file_path not in seen[change_id]["files"]:
                    seen[change_id]["files"].append(file_path)
                continue

            def _props(node: Any) -> dict:
                return node.get("properties", {}) if node and isinstance(node, dict) else {}

            dc_props = _props(row[0])
            pkg_props = _props(row[1]) if len(row) > 1 else {}
            fv_props = _props(row[2]) if len(row) > 2 else {}
            tv_props = _props(row[3]) if len(row) > 3 else {}
            c_props = _props(row[4]) if len(row) > 4 else {}
            fix_props = _props(row[5]) if len(row) > 5 else {}

            seen[change_id] = {
                "package": pkg_props.get("purl", ""),
                "from_version": fv_props.get("version"),
                "to_version": tv_props.get("version", ""),
                "manifest_path": dc_props.get("manifest_path", ""),
                "commit_sha": c_props.get("sha"),
                "fix_id": fix_props.get("fix_id"),
                "relationship": fv_props.get("relationship") or tv_props.get("relationship") or "unknown",
                "files": [file_path],
            }
    return list(seen.values())


# @spec DEPG-DRE-005
def _format_dependency_change_explanation(
    package: str,
    from_version: str | None,
    to_version: str,
    manifest_path: str,
    files: list[str],
) -> str:
    """Mechanical string template — deliberately not natural language, same
    discipline quick_investigation_summary already applies (no LLM call)."""
    version_part = f"{from_version} -> {to_version}" if from_version else to_version
    files_part = ", ".join(files)
    return f"{package} {version_part} ({manifest_path}), used by {files_part}"


# @spec TCLINK-DRE-002
async def _traverse_test_files_to_recent_failures(
    test_paths: list[str],
    project_slug: str,
    client: QuineClient,
) -> list[dict]:
    """Return TestFailures reachable from the given test paths via
    TestFile <-[:EXECUTES]- TestExecution <-[:OCCURRED_IN]- TestFailure.
    Deduplicated by TestFailure id. Not sorted or capped by recency — same
    rationale as _traverse_files_to_recent_dependency_changes: a candidate
    only ever reaches this traversal by already being an anchored test file,
    so recency isn't the relevance signal here."""
    seen: dict[Any, dict] = {}
    for test_path in test_paths:
        rows = await client.query(
            "MATCH (tf) WHERE id(tf) = idFrom('test-file', $project_slug, $file_path) "
            "MATCH (tf)<-[:EXECUTES]-(te)<-[:OCCURRED_IN]-(failure) "
            "RETURN failure, te",
            {"project_slug": project_slug, "file_path": test_path},
        )
        for row in rows:
            if not row or not row[0] or not isinstance(row[0], dict):
                continue
            failure_id = row[0].get("id")
            if failure_id in seen:
                continue
            failure_props = row[0].get("properties", {})
            te_props = row[1].get("properties", {}) if len(row) > 1 and row[1] and isinstance(row[1], dict) else {}
            seen[failure_id] = {
                "test_path": test_path,
                "classname": failure_props.get("classname") or te_props.get("classname", ""),
                "test_name": failure_props.get("test_name") or te_props.get("test_name", ""),
                "run_id": failure_props.get("run_id") or te_props.get("run_id", ""),
                "failure_type": failure_props.get("failure_type", ""),
                "message": failure_props.get("message", ""),
                "observed_at": failure_props.get("observed_at", ""),
            }
    return list(seen.values())


# @spec TCLINK-DRE-004
def _format_test_failure_explanation(
    classname: str, test_name: str, run_id: str, message: str
) -> str:
    """Mechanical string template — no LLM call, same discipline as
    _format_dependency_change_explanation."""
    return f"{classname}::{test_name} failed in run {run_id}: {message}"


# @spec DRE-TRAV-008
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
    source_paths = list(
        dict.fromkeys(
            row[2]["properties"]["repo_path"]
            for row in rows
            if len(row) > 2
            and row[2]
            and isinstance(row[2], dict)
            and row[2].get("properties", {}).get("repo_path")
        )
    )

    test_rows = await client.query(
        "MATCH (f) WHERE id(f) = idFrom('feature', $project_slug, $feature_slug) "
        "OPTIONAL MATCH (f)-[:HAS_TEST]->(file) "
        "WHERE file.node_type = 'TestFile' "
        "RETURN file",
        {"project_slug": project_slug, "feature_slug": feature_slug},
    )
    test_paths = list(
        dict.fromkeys(
            row[0]["properties"]["repo_path"]
            for row in test_rows
            if row
            and row[0]
            and isinstance(row[0], dict)
            and row[0].get("properties", {}).get("repo_path")
        )
    )

    if source_paths or test_paths:
        return source_paths, test_paths, "feature"

    # Fallback: treat slug as a Module slug
    rows = await client.query(
        "MATCH (m) WHERE id(m) = idFrom('module', $project_slug, $feature_slug) "
        "OPTIONAL MATCH (m)-[:DEFINED_IN]->(file) "
        "RETURN m, file",
        {"project_slug": project_slug, "feature_slug": feature_slug},
    )
    module_paths = list(
        dict.fromkeys(
            row[1]["properties"]["repo_path"]
            for row in rows
            if len(row) > 1
            and row[1]
            and isinstance(row[1], dict)
            and row[1].get("properties", {}).get("repo_path")
        )
    )

    # Walk up to the parent Feature to get its HAS_TEST → TestFile edges.
    test_rows = await client.query(
        "MATCH (m) WHERE id(m) = idFrom('module', $project_slug, $feature_slug) "
        "OPTIONAL MATCH (f)-[:IMPLEMENTED_BY]->(m) "
        "OPTIONAL MATCH (f)-[:HAS_TEST]->(tfile) "
        "WHERE tfile.node_type = 'TestFile' "
        "RETURN tfile",
        {"project_slug": project_slug, "feature_slug": feature_slug},
    )
    module_test_paths = list(
        dict.fromkeys(
            row[0]["properties"]["repo_path"]
            for row in test_rows
            if row
            and row[0]
            and isinstance(row[0], dict)
            and row[0].get("properties", {}).get("repo_path")
        )
    )

    return module_paths, module_test_paths, "module"


async def _traverse_error_to_known_issues(
    normalized_error: str,
    project_slug: str,
    client: QuineClient,
) -> list[tuple[str, dict[str, str]]]:
    """Return (quine_node_id, props) for each KnownIssue reachable from this error."""
    rows = await client.query(
        "MATCH (e) WHERE e.node_type = 'ErrorSignature' AND e.project_slug = $project_slug "
        "AND e.normalized_error = $normalized_error "
        "MATCH (e)<-[:HAS_ERROR]-(ki) WHERE ki.node_type = 'KnownIssue' "
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
        "MATCH (ki) WHERE id(ki) = $ki_node_id "
        "MATCH (ki)-[:RESOLVED_BY]->(fix) WHERE fix.node_type = 'Fix' AND fix.project_slug = $project_slug "
        "RETURN fix",
        {"project_slug": project_slug, "ki_node_id": ki_node_id},
    )
    return [
        row[0]["properties"] for row in rows if row and row[0].get("properties", {}).get("fix_id")
    ]


async def _fetch_fix_commit_sha(
    fix_id: str,
    project_slug: str,
    client: QuineClient,
) -> str:
    """Return the short commit SHA for a fix via Fix-[:IMPLEMENTED_IN]->Commit, or ''."""
    rows = await client.query(
        "MATCH (f) WHERE id(f) = idFrom('fix', $project_slug, $fix_id) "
        "MATCH (f)-[:IMPLEMENTED_IN]->(c) WHERE c.node_type = 'Commit' "
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
        "MATCH (ci) WHERE id(ci) = $issue_id AND ci.node_type = 'CustomerIssue' "
        "MATCH (ci)-[:HAS_SIMILARITY_MATCH]->(sm)-[:MATCHES]->(ki) "
        "WHERE sm.node_type = 'SimilarityMatch' AND ki.node_type = 'KnownIssue' "
        "AND ki.project_slug = $project_slug AND sm.review_status IN ['candidate', 'confirmed'] "
        "RETURN ki, sm.review_status",
        {"issue_id": issue_id, "project_slug": project_slug},
    )
    results = []
    for row in rows:
        if not row or len(row) < 2:
            continue
        ki_props = row[0].get("properties", {})
        review_status = (
            row[1].get("review_status", "candidate") if isinstance(row[1], dict) else str(row[1])
        )
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
    feature_source_files: dict[str, list[str]] | None = None,
    on_progress: Callable[[str, "DebugPacket"], None] | None = None,
    skip_summary: bool = False,
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

    # @spec DRE-STREAM-001
    if on_progress is not None:
        on_progress(
            "loading",
            DebugPacket(
                issue=IssueSummary(
                    summary=issue.summary,
                    anchors=IssueAnchors(features=[], errors=[], symptoms=[]),
                ),
                affected_areas=[],
                relevant_files=[],
                relevant_tests=[],
                known_issues=[],
                prior_fixes=[],
                recent_commits=[],
                scored_candidates=[],
                summary="",
            ),
        )

    # Anchor extraction — graph-first
    # @spec DRE-ANCH-001, DRE-ANCH-002, DRE-ANCH-003
    try:
        feature_slugs, error_sigs = await _graph_anchors(issue_id, project_slug, client)
    except Exception as exc:
        raise DREGraphUnavailableError(
            f"Quine unreachable during anchor extraction: {exc}"
        ) from exc

    symptoms: list[str] = []
    mentioned_files: list[str] = []

    if not feature_slugs and not error_sigs:
        # @spec DRE-ANCH-004, DRE-ANCH-005, DRE-ANCH-006, DRE-ANCH-007
        if issue.raw_text is None:
            raise DREAnchorError(
                f"CustomerIssue id={issue_id} has no graph anchors and no raw_text"
            )

        # @spec DRE-ANCH-009
        pre_matched = _pre_match_modules(issue.raw_text, module_source_files or {}, module_elements)

        try:
            parse_result = await gateway.parse_ticket(
                issue.raw_text,
                project_slug,
                backend=backend,
                valid_slugs=valid_slugs,
                feature_slugs=feature_slugs,
                module_slugs=module_slugs,
                feature_descriptions=feature_descriptions,
                module_descriptions=module_descriptions,
                module_elements=module_elements,
                module_source_files=module_source_files,
            )
            # @spec DRE-ANCH-004 — LLM is the authority when it succeeds; pre_matched is fallback only
            feature_slugs = list(parse_result.feature_slugs)
            error_sigs = list(parse_result.error_signatures)
            symptoms = list(parse_result.symptoms)
            mentioned_files = list(parse_result.mentioned_files)
        except LLMUnavailableError as exc:
            raise DRELLMUnavailableError(f"LLM gateway unreachable: {exc}") from exc
        except LLMGatewayError:
            # @spec DRE-ANCH-006 — bad/rejected LLM output (4xx, bad JSON): fall back to pre-match
            feature_slugs = list(pre_matched)

        # @spec DRE-ANCH-010 — mechanical validation pass before Quine traversal
        if valid_slugs:
            feature_slugs = [s for s in feature_slugs if s in valid_slugs]

    # Accumulators
    ki_counts: dict[str, int] = {}
    ki_meta: dict[str, dict[str, str]] = {}
    fix_counts: dict[str, int] = {}
    fix_meta: dict[str, dict[str, str]] = {}
    file_evidence: dict[str, list[EvidenceItem]] = {}  # source files
    test_file_evidence: dict[str, list[EvidenceItem]] = {}  # test files
    # @spec DRE-TESTCOV-001 — HAS_TEST coverage tracked informationally, not
    # as scored evidence; see the covered_tests filtering step below.
    covered_tests_map: dict[str, list[str]] = {}
    matched_anchors = 0
    resolved_module_slugs: list[str] = []
    resolved_feature_slugs: list[str] = []

    # @spec DRE-TRAV-001, DRE-TRAV-009
    for slug in feature_slugs:
        try:
            src_paths, tst_paths, resolved_as = await _traverse_feature_to_files(
                slug, project_slug, client
            )
        except Exception as exc:
            raise DREGraphUnavailableError(f"Quine unreachable during traversal: {exc}") from exc

        if src_paths or tst_paths:
            matched_anchors += 1
            # @spec DRE-TRAV-009
            feature_primary_paths = (
                set(feature_source_files.get(slug, [])) if feature_source_files else None
            )
            for path in src_paths:
                is_primary = (
                    resolved_as == "module"
                    or feature_primary_paths is None
                    or path in feature_primary_paths
                )
                _add_evidence(
                    file_evidence,
                    path,
                    EvidenceItem(
                        type="feature_primary_file" if is_primary else "feature_anchor",
                        score=9.0 if is_primary else 3.0,
                        explanation=slug,
                    ),
                )
            # @spec DRE-TESTCOV-001 — bare HAS_TEST coverage no longer earns
            # a scored EvidenceItem (found live: a test file covered by
            # multiple features stacked one test_coverage hit per feature via
            # geometric decay — e.g. two hits at 7.0 + 7.0*0.5 = 10.5 — which
            # promoted a file with zero ticket-specific evidence into MEDIUM
            # confidence, ahead of genuinely relevant candidates). Coverage
            # is tracked here for the informational covered_tests field
            # instead; test_file_evidence[path] is still seeded (empty) so
            # the file remains eligible for real evidence — ticket_mention,
            # recent_commit, commit_message_match, function_anchor_match —
            # from later steps in this pipeline.
            for path in tst_paths:
                test_file_evidence.setdefault(path, [])
                slugs = covered_tests_map.setdefault(path, [])
                if slug not in slugs:
                    slugs.append(slug)
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
                raise DREGraphUnavailableError(
                    f"Quine unreachable during traversal: {exc}"
                ) from exc
            for fix_props in fix_props_list:
                fid = fix_props["fix_id"]
                _accumulate_match_count(fix_counts, fid, 1)
                fix_meta[fid] = fix_props

    # @spec DRE-TRAV-004, DRE-SCORE-002
    try:
        sim_results = await _traverse_similarity(issue_id, project_slug, client)
    except Exception as exc:
        raise DREGraphUnavailableError(
            f"Quine unreachable during similarity traversal: {exc}"
        ) from exc
    for props, review_status in sim_results:
        ki_id = props["issue_id"]
        weight = 2 if review_status == "confirmed" else 1
        _accumulate_match_count(ki_counts, ki_id, weight)
        ki_meta.setdefault(ki_id, props)

    # Seed explicitly mentioned files from LLM parse into the right bucket
    for fpath in mentioned_files:
        item = EvidenceItem(
            type="ticket_mention",
            score=10.0,
            explanation="File explicitly mentioned in ticket text",
        )
        if _is_test_path(fpath):
            _add_evidence(test_file_evidence, fpath, item)
        else:
            _add_evidence(file_evidence, fpath, item)

    # Sort and cap ki/fix (unchanged)
    # @spec DRE-SCORE-003, DRE-SCORE-004
    ki_items = _sort_and_cap([{"id": k, "match_count": v} for k, v in ki_counts.items()], _KI_CAP)
    fix_items = _sort_and_cap(
        [{"id": k, "match_count": v} for k, v in fix_counts.items()], _FIX_CAP
    )

    # Preliminary file lists (pre-commit evidence) for commit traversal
    prelim_source = list(file_evidence.keys())
    prelim_tests = list(test_file_evidence.keys())

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
        prior_fixes.append(
            PriorFix(
                id=fid,
                commit=commit_sha,
                summary=fix_meta[fid].get("summary", ""),
            )
        )

    affected_areas: list[AffectedArea] = []
    for slug in resolved_feature_slugs:
        affected_areas.append(AffectedArea(type="feature", id=f"feature:{slug}", name=slug))
    for slug in resolved_module_slugs:
        affected_areas.append(AffectedArea(type="module", id=f"module:{slug}", name=slug))

    all_file_paths = prelim_source + prelim_tests
    raw_commits = await _traverse_files_to_recent_commits(all_file_paths, project_slug, client)

    anchor_tokens = _build_anchor_tokens(feature_slugs, error_sigs, symptoms)

    # @spec DRE-TOKEN-002
    symptom_error_tokens = _build_anchor_tokens([], error_sigs, symptoms)
    # @spec DRE-ELEM-001, DRE-ELEM-002, DRE-ELEM-003, DRE-ELEM-004
    matched_elements: list[str] = []
    if symptom_error_tokens and module_elements and module_source_files:
        for slug in resolved_module_slugs:
            elements = module_elements.get(slug, [])
            files = module_source_files.get(slug, [])
            slug_matches = [elem for elem in elements if _tokenize(elem) & symptom_error_tokens]
            if slug_matches and files:
                matched_elements.extend(slug_matches)
                elem_names = ", ".join(slug_matches[:3])
                for fpath in files:
                    ev_map = (
                        test_file_evidence
                        if fpath in test_file_evidence
                        else (file_evidence if fpath in file_evidence else None)
                    )
                    if ev_map is not None:
                        _add_evidence(
                            ev_map,
                            fpath,
                            EvidenceItem(
                                type="element_anchor_match",
                                score=6.0,
                                explanation=elem_names,
                            ),
                        )
                for elem in slug_matches:
                    anchor_tokens.update(_tokenize(elem))

    # @spec DRE-TOKEN-003
    func_anchor_tokens = symptom_error_tokens.copy()
    for elem in matched_elements:
        func_anchor_tokens.update(_tokenize(elem))

    # @spec DRE-FUNC-001, DRE-FUNC-002, DRE-FUNC-003, DRE-CAND-005, DRE-CAND-007
    for c in raw_commits:
        sha_short = c.get("sha", "")[:7]
        message = (c.get("message") or "").splitlines()[0] if c.get("message") else ""
        message_tokens = extract_text_tokens(message) if message else set()
        matched_message_tokens = message_tokens & anchor_tokens if anchor_tokens else set()
        for fpath in c.get("files_touched", []):
            evidence_map = (
                test_file_evidence
                if fpath in test_file_evidence
                else (file_evidence if fpath in file_evidence and _is_source_path(fpath) else None)
            )
            if evidence_map is None:
                continue
            _add_evidence(
                evidence_map,
                fpath,
                EvidenceItem(
                    type="recent_commit",
                    score=1.5,
                    explanation=f"Touched in recent commit {sha_short}",
                    commit_sha=sha_short,
                ),
            )
            if matched_message_tokens:
                _add_evidence(
                    evidence_map,
                    fpath,
                    EvidenceItem(
                        type="commit_message_match",
                        score=9.0,
                        explanation=f"{message[:80]} · {sha_short}",
                        commit_sha=sha_short,
                    ),
                )
            if func_anchor_tokens:
                hunk_data = c.get("file_hunk_data", {}).get(fpath, [])
                matched = _matching_defs(hunk_data, func_anchor_tokens)
                if matched:
                    names = ", ".join(matched[:3])
                    _add_evidence(
                        evidence_map,
                        fpath,
                        EvidenceItem(
                            type="function_anchor_match",
                            score=6.0,
                            explanation=f"{names} · {sha_short}",
                            commit_sha=sha_short,
                        ),
                    )

    # @spec DEPG-DRE-001, DEPG-DRE-002, DEPG-DRE-003, DEPG-DRE-006
    raw_dependency_changes = await _traverse_files_to_recent_dependency_changes(
        all_file_paths, project_slug, client
    )
    for dep_change in raw_dependency_changes:
        for fpath in dep_change.get("files", []):
            evidence_map = (
                test_file_evidence
                if fpath in test_file_evidence
                else (file_evidence if fpath in file_evidence else None)
            )
            if evidence_map is None:
                # DEPG-DRE-002 — never discovers a new file; only files
                # already anchored by feature/module resolution receive this.
                continue
            _add_evidence(
                evidence_map,
                fpath,
                EvidenceItem(
                    type="dependency_change",
                    score=5.0,
                    explanation=_format_dependency_change_explanation(
                        package=dep_change.get("package", ""),
                        from_version=dep_change.get("from_version"),
                        to_version=dep_change.get("to_version", ""),
                        manifest_path=dep_change.get("manifest_path", ""),
                        files=dep_change.get("files", []),
                    ),
                ),
            )

    # @spec TCLINK-DRE-001, TCLINK-DRE-002, TCLINK-DRE-005, TCLINK-SCOPE-002
    raw_test_failures = await _traverse_test_files_to_recent_failures(
        list(test_file_evidence.keys()), project_slug, client
    )
    for tf in raw_test_failures:
        test_path = tf.get("test_path", "")
        if test_path not in test_file_evidence:
            # TCLINK-DRE-005 — scoped to the TestFile candidate only; never
            # discovers a new file or propagates to a source file.
            continue
        _add_evidence(
            test_file_evidence,
            test_path,
            EvidenceItem(
                type="recent_test_failure",
                score=9.0,
                explanation=_format_test_failure_explanation(
                    classname=tf.get("classname", ""),
                    test_name=tf.get("test_name", ""),
                    run_id=tf.get("run_id", ""),
                    message=tf.get("message", ""),
                ),
            ),
        )

    # @spec DRE-TESTCOV-002 — a test file that earned real evidence elsewhere
    # in this pipeline (test_file_evidence[path] non-empty) stays a scored
    # candidate; a covered-but-otherwise-unevidenced test file is pulled out
    # of test_file_evidence (so it doesn't appear as a zero-score entry in
    # scored_candidates/relevant_tests) and reported only in covered_tests.
    covered_tests_list: list[CoveredTest] = []
    for path in sorted(covered_tests_map):
        if test_file_evidence.get(path):
            continue
        test_file_evidence.pop(path, None)
        covered_tests_list.append(CoveredTest(path=path, covering_slugs=covered_tests_map[path]))

    # Build scored candidates and derive ordered file lists
    source_candidates = _build_scored_candidates(file_evidence, "source", _FILE_CAP)
    test_candidates = _build_scored_candidates(test_file_evidence, "test", _FILE_CAP)
    scored_candidates = sorted(
        source_candidates + test_candidates, key=lambda c: c.score, reverse=True
    )

    relevant_files = [c.path for c in source_candidates]
    relevant_tests = [c.path for c in test_candidates]

    issue_obj = IssueSummary(
        summary=issue.summary,
        anchors=IssueAnchors(
            features=resolved_feature_slugs,
            errors=error_sigs,
            symptoms=symptoms,
        ),
    )
    recent_commits_list = [
        RecentCommit(
            sha=c.get("sha", ""),
            timestamp=c.get("timestamp", ""),
            author_name=c.get("author_name", ""),
            message=c.get("message", ""),
            files_touched=c.get("files_touched", []),
        )
        for c in raw_commits
    ]
    # @spec DEPG-DRE-004
    recent_dependency_changes_list = [
        RecentDependencyChange(
            package=d.get("package", ""),
            from_version=d.get("from_version"),
            to_version=d.get("to_version", ""),
            manifest_path=d.get("manifest_path", ""),
            commit_sha=d.get("commit_sha"),
            fix_id=d.get("fix_id"),
            relationship=d.get("relationship", "unknown"),
            files=d.get("files", []),
            explanation=_format_dependency_change_explanation(
                package=d.get("package", ""),
                from_version=d.get("from_version"),
                to_version=d.get("to_version", ""),
                manifest_path=d.get("manifest_path", ""),
                files=d.get("files", []),
            ),
        )
        for d in raw_dependency_changes
    ]
    # @spec TCLINK-DRE-003, TCLINK-DRE-004
    recent_test_failures_list = [
        RecentTestFailure(
            test_path=tf.get("test_path", ""),
            classname=tf.get("classname", ""),
            test_name=tf.get("test_name", ""),
            run_id=tf.get("run_id", ""),
            failure_type=tf.get("failure_type", ""),
            message=tf.get("message", ""),
            observed_at=tf.get("observed_at", ""),
            explanation=_format_test_failure_explanation(
                classname=tf.get("classname", ""),
                test_name=tf.get("test_name", ""),
                run_id=tf.get("run_id", ""),
                message=tf.get("message", ""),
            ),
        )
        for tf in raw_test_failures
    ]

    # @spec DRE-STREAM-002
    if on_progress is not None:
        on_progress(
            "partial",
            DebugPacket(
                issue=issue_obj,
                affected_areas=affected_areas,
                relevant_files=relevant_files,
                relevant_tests=relevant_tests,
                known_issues=known_issues,
                prior_fixes=prior_fixes,
                recent_commits=recent_commits_list,
                recent_dependency_changes=recent_dependency_changes_list,
                recent_test_failures=recent_test_failures_list,
                covered_tests=covered_tests_list,
                scored_candidates=scored_candidates,
                summary="",
            ),
        )

    # @spec DRE-SUMM-001, DRE-SUMM-002
    raw_text = issue.raw_text or issue.summary
    if skip_summary:
        summary = issue.summary
    else:
        try:
            summary = await gateway.summarise_packet(
                issue_text=raw_text,
                module_slugs=[a.name for a in affected_areas if a.type == "module"],
                error_signatures=error_sigs,
                symptoms=symptoms,
                relevant_files=relevant_files,
                relevant_tests=relevant_tests,
                matched_elements=matched_elements,
                recent_commits=[
                    {
                        "timestamp": c.get("timestamp", ""),
                        "author_name": c.get("author_name", ""),
                        "message": c.get("message", ""),
                    }
                    for c in raw_commits
                ],
                known_issues=[ki.summary for ki in known_issues],
                backend=backend,
            )
        except Exception:
            summary = issue.summary

    return DebugPacket(
        issue=issue_obj,
        affected_areas=affected_areas,
        relevant_files=relevant_files,
        relevant_tests=relevant_tests,
        known_issues=known_issues,
        prior_fixes=prior_fixes,
        recent_commits=recent_commits_list,
        recent_dependency_changes=recent_dependency_changes_list,
        recent_test_failures=recent_test_failures_list,
        covered_tests=covered_tests_list,
        scored_candidates=scored_candidates,
        summary=summary,
    )
