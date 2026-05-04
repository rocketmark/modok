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
class DebugPacket:
    issue: IssueSummary
    affected_areas: list[AffectedArea]
    relevant_files: list[str]
    relevant_tests: list[str]
    known_issues: list[KnownIssueRef]
    prior_fixes: list[PriorFix]
    next_steps: list[str]
