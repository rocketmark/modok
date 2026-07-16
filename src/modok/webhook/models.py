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
    pr_url: str | None = None
    merge_commit_sha: str | None = None
    closing_issue_numbers: list[str] = field(default_factory=list)
    is_open_dependabot: bool = False


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
class MilestoneData:
    """Evidence for one accumulating Investigation (see docs/llds/continuous-ci-ingestion.md
    § Investigation and Milestone Model) — distinct from InvestigationData, whose
    identity scheme mints one Investigation per standing-query firing.
    workflow_run_id/test_failure_id/error_signature/workflow_name/head_sha are
    presentation-only: they never enter Investigation identity (only
    source_system + ticket_id do)."""
    source_system: str
    ticket_id: str
    milestone_kind: str
    standing_query_name: str
    workflow_run_id: str = ""
    test_failure_id: str = ""
    error_signature: str = ""
    workflow_name: str = ""
    head_sha: str = ""


@dataclass(frozen=True, eq=True)
class IngestEvent:
    kind: Literal["customer_issue", "fix", "investigation", "milestone", "skip"]
    project_slug: str
    data: CustomerIssueData | FixData | InvestigationData | MilestoneData | None = field(
        default=None
    )
