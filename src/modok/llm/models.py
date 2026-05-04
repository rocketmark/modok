from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TicketParseResult:
    feature_slugs: list[str]
    error_signatures: list[str]
    environment: dict[str, str]
    symptoms: list[str]
    confidence: float
    raw_response: str
    mentioned_files: list[str] = field(default_factory=list)


@dataclass
class MetadataProposal:
    proposed_fields: dict[str, Any]
    confidence: float
    evidence: str
    raw_response: str


@dataclass
class SimilarityProposal:
    known_issue_id: str
    score: float
    method: str
    evidence_anchors: list[str]
    raw_response: str


@dataclass
class KnownIssueSummary:
    known_issue_id: str
    summary: str
    error_signatures: list[str] = field(default_factory=list)
