from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class AnchorSet:
    feature_slugs: list[str] = field(default_factory=list)
    error_signatures: list[str] = field(default_factory=list)
    symptoms: list[str] = field(default_factory=list)


@dataclass
class KnownIssueRef:
    known_issue_id: str
    summary: str
    status: str
    match_count: int


@dataclass
class FixRef:
    fix_id: str
    summary: str
    kind: str
    match_count: int


@dataclass
class FileRef:
    repo_path: str
    match_count: int


@dataclass
class CommitRef:
    sha: str
    message: str
    author_name: str
    timestamp: str
    files_touched: list[str] = field(default_factory=list)


@dataclass
class EvidenceAnchor:
    anchor_type: str        # "feature" | "error_signature"
    anchor_value: str
    matched_node_ids: list[str] = field(default_factory=list)


@dataclass
class DebugPacket:
    issue_summary: str
    anchors: AnchorSet
    anchor_count: int
    known_issues: list[KnownIssueRef]
    recent_fixes: list[FixRef]
    relevant_files: list[FileRef]
    recent_commits: list[CommitRef]
    evidence: list[EvidenceAnchor]
    confidence: float
