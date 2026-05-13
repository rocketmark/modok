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


@dataclass(frozen=True, eq=True)
class FixData:
    fix_id: str
    summary: str
    kind: str = "pull-request"


@dataclass(frozen=True, eq=True)
class IngestEvent:
    kind: Literal["customer_issue", "fix", "skip"]
    project_slug: str
    data: CustomerIssueData | FixData | None = field(default=None)
