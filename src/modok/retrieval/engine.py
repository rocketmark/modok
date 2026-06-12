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
    DebugPacket,
    EvidenceItem,
    IssueAnchors,
    IssueSummary,
    KnownIssueRef,
    PriorFix,
    RecentCommit,
    ScoredCandidate,
)

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
}


def _is_source_path(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in _SOURCE_EXTS


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
        text_tokens: set[str] = set()
        for word in re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]{2,}\b", text):
            text_tokens.update(_tokenize(word))
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

_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


# @spec DRE-TOKEN-001
def _tokenize(name: str) -> set[str]:
    """Split a camelCase/snake_case/kebab-case identifier into lowercase tokens (length > 2)."""
    parts = re.split(r"[_\-\s]+", name)
    tokens: set[str] = set()
    for part in parts:
        for sub in _CAMEL_SPLIT_RE.split(part):
            if len(sub) > 2:
                tokens.add(sub.lower())
    return tokens


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


# @spec DRE-CAND-001, DRE-CAND-002
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
    total += 3.0 * min(len(by_type) - 1, 4)
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
        if len(row) > 2
        and row[2]
        and isinstance(row[2], dict)
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
        if row
        and row[0]
        and isinstance(row[0], dict)
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
        if len(row) > 1
        and row[1]
        and isinstance(row[1], dict)
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
        if row
        and row[0]
        and isinstance(row[0], dict)
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
                _add_evidence(
                    file_evidence,
                    path,
                    EvidenceItem(
                        type="feature_anchor",
                        score=7.0,
                        explanation=slug,
                    ),
                )
            for path in tst_paths:
                _add_evidence(
                    test_file_evidence,
                    path,
                    EvidenceItem(
                        type="test_coverage",
                        score=8.0,
                        explanation=slug,
                    ),
                )
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

    # @spec DRE-FUNC-001, DRE-FUNC-002, DRE-FUNC-003
    for c in raw_commits:
        sha_short = c.get("sha", "")[:7]
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
                    score=4.0,
                    explanation=f"Touched in recent commit {sha_short}",
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
                        ),
                    )

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
        scored_candidates=scored_candidates,
        summary=summary,
    )
