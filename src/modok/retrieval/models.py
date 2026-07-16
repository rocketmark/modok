from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class IssueAnchors:
    features: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    symptoms: list[str] = field(default_factory=list)


@dataclass
class IssueSummary:
    summary: str
    anchors: IssueAnchors


@dataclass
class AffectedArea:
    type: str  # "feature" or "module"
    id: str  # "feature:shtp-receiver"
    name: str  # slug used as display name


@dataclass
class KnownIssueRef:
    id: str
    summary: str


@dataclass
class PriorFix:
    id: str
    commit: str
    summary: str


@dataclass
class RecentCommit:
    sha: str
    timestamp: str
    author_name: str
    message: str
    files_touched: list[str] = field(default_factory=list)


@dataclass
class RecentDependencyChange:
    package: str  # purl, e.g. "pkg:pypi/bleak"
    from_version: str | None
    to_version: str
    manifest_path: str
    commit_sha: str | None
    fix_id: str | None
    relationship: str
    files: list[str] = field(default_factory=list)
    explanation: str = ""


@dataclass
class CoveredTest:
    """A test file reached via Feature/Module HAS_TEST traversal with no
    other evidence tying it to this specific ticket — informational only,
    not part of scored_candidates/relevant_tests. A test file that also
    earns real evidence (ticket_mention, a matching recent commit, ...)
    appears in scored_candidates instead, not here (see DRE-TESTCOV-002)."""
    path: str
    covering_slugs: list[str] = field(default_factory=list)


@dataclass
class RecentTestFailure:
    test_path: str
    classname: str
    test_name: str
    run_id: str
    failure_type: str
    message: str
    observed_at: str
    explanation: str = ""


@dataclass
class EvidenceItem:
    type: str
    score: float
    explanation: str
    # Set only for commit-derived evidence (recent_commit, commit_message_match,
    # function_anchor_match) so the formatter can group evidence by commit
    # instead of relying on parsing the commit SHA out of `explanation` text.
    commit_sha: str | None = None


@dataclass
class ScoredCandidate:
    path: str
    kind: str  # "source" | "test"
    score: float
    confidence: str  # "high" | "medium" | "low"
    evidence: list[EvidenceItem] = field(default_factory=list)


@dataclass
class DebugPacket:
    issue: IssueSummary
    affected_areas: list[AffectedArea]
    relevant_files: list[str]
    relevant_tests: list[str]
    known_issues: list[KnownIssueRef]
    prior_fixes: list[PriorFix]
    recent_commits: list[RecentCommit] = field(default_factory=list)
    recent_dependency_changes: list[RecentDependencyChange] = field(default_factory=list)
    recent_test_failures: list[RecentTestFailure] = field(default_factory=list)
    covered_tests: list[CoveredTest] = field(default_factory=list)
    scored_candidates: list[ScoredCandidate] = field(default_factory=list)
    summary: str = ""
