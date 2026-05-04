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
    type: str    # "feature" or "module"
    id: str      # "feature:shtp-receiver"
    name: str    # slug used as display name


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
class EvidenceItem:
    type: str
    score: float
    explanation: str


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
    scored_candidates: list[ScoredCandidate] = field(default_factory=list)
    summary: str = ""
