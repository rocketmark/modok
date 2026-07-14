from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class WebhookConfig(BaseModel):
    port: int = 4242
    host: str = "127.0.0.1"
    github_secret: str = ""
    bearer_token: str = ""
    enabled_sources: list[str] | None = None  # None = all adapters active
    github_poll_enabled: bool = False
    github_poll_interval_seconds: float = 30


# ---------------------------------------------------------------------------
# Ingest event — common currency between all adapters and the pipeline
# ---------------------------------------------------------------------------


@dataclass(frozen=True, eq=True)
class CustomerIssueData:
    ticket_id: str
    summary: str
    raw_text: str
    status: str
    source_system: str
    ticket_kind: str | None = None


@dataclass(frozen=True, eq=True)
class FixData:
    fix_id: str
    summary: str
    kind: str = "pull-request"


@dataclass(frozen=True, eq=True)
class InvestigationData:
    source_system: str
    ticket_id: str
    standing_query_name: str
    # Optional: only the actionable-issue-pattern match (a known, already-fixed
    # defect) populates these. Broader patterns (new-bug-report-pattern,
    # error-flagged-pattern) fire on a CustomerIssue alone and leave them "".
    known_issue_id: str = ""
    fix_id: str = ""


@dataclass(frozen=True, eq=True)
class IngestEvent:
    kind: Literal["customer_issue", "fix", "investigation", "skip"]
    project_slug: str
    data: CustomerIssueData | FixData | InvestigationData | None = field(default=None)
