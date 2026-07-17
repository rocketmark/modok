"""The IngestEvent dispatcher — the one normalized-event boundary every
adapter (push and pull alike) routes through. Kept dependency-free of
modok.webhook.router/adapters so modok.ingestion.github can import
run_ingest_event without a circular import (router -> adapters ->
modok.ingestion.github -> here). The "investigation" and "milestone"
branches call back into modok.webhook.server lazily (function-body imports)
since their supporting helpers (_process_investigation, _process_milestone,
GitHub write-back) legitimately belong next to build_app's routes and the
existing patch targets in tests assume modok.webhook.server as their home.
See docs/llds/continuous-ci-ingestion.md § Prerequisite: Unified GitHub
Event Routing."""
# @spec WH-IDEM-003, WH-EXT-003, GHING-ROUTE-001, GHING-ROUTE-002,
#       GHING-ROUTE-003, GHING-ROUTE-004

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from modok.quine.models import CustomerIssue, Fix
from modok.webhook.models import (
    CustomerIssueData,
    FileEscalationData,
    FixData,
    IngestEvent,
    InvestigationData,
    MilestoneData,
    RootCauseEscalationData,
)


def _pr_number_from_fix_id(fix_id: str) -> str:
    return fix_id[3:] if fix_id.startswith("gh-") else fix_id


# @spec WH-IDEM-003, WH-EXT-003
def run_ingest_event(event: IngestEvent, quine_client: Any) -> int:
    """Write an IngestEvent to Quine. Returns number of nodes written.

    Sync — called via asyncio.to_thread from async callers. Routes purely on
    event.kind; no branching on adapter identity, source system, or origin
    (WH-EXT-003, WH-EXT-004).
    """
    if event.kind == "customer_issue":
        assert isinstance(event.data, CustomerIssueData)
        node = CustomerIssue(
            node_type="CustomerIssue",
            project_slug=event.project_slug,
            source_system=event.data.source_system,
            ticket_id=event.data.ticket_id,
            summary=event.data.summary,
            raw_text=event.data.raw_text,
            status=event.data.status,
            ticket_kind=event.data.ticket_kind,
            created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        asyncio.run(quine_client.upsert_node(node))
        from modok.webhook.server import _link_anchors_resilient

        asyncio.run(_link_anchors_resilient(quine_client, event.project_slug, node))
        return 1

    # @spec GHING-ROUTE-003, GHING-ROUTE-004 — full PR handling: open Dependabot
    # PRs become a CustomerIssue, merged PRs become a Fix plus IMPLEMENTED_IN /
    # RESOLVED_BY edges. Previously this lived only in GithubIngester.ingest_pr;
    # the poll path now dispatches here too instead of mutating inline.
    if event.kind == "fix":
        assert isinstance(event.data, FixData)
        data = event.data

        if data.is_open_dependabot:
            node = CustomerIssue(
                node_type="CustomerIssue",
                project_slug=event.project_slug,
                source_system="github",
                ticket_id=_pr_number_from_fix_id(data.fix_id),
                summary=data.summary,
                raw_text="",
                status="open",
                created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            asyncio.run(quine_client.upsert_node(node))
            return 1

        node = Fix(
            node_type="Fix",
            project_slug=event.project_slug,
            fix_id=data.fix_id,
            summary=data.summary,
            kind=data.kind,
            pr_url=data.pr_url,
        )
        asyncio.run(quine_client.upsert_node(node))

        if data.merge_commit_sha:
            commit_parts = ("commit", event.project_slug, data.merge_commit_sha)
            if asyncio.run(quine_client.node_exists_by_parts(commit_parts)):
                asyncio.run(
                    quine_client.write_edge_by_parts(
                        ("fix", event.project_slug, data.fix_id),
                        "IMPLEMENTED_IN",
                        commit_parts,
                    )
                )

        for issue_num in data.closing_issue_numbers:
            ci_parts = ("customer-issue", event.project_slug, "github", str(issue_num))
            if asyncio.run(quine_client.node_exists_by_parts(ci_parts)):
                asyncio.run(
                    quine_client.write_edge_by_parts(
                        ci_parts, "RESOLVED_BY", ("fix", event.project_slug, data.fix_id)
                    )
                )

        return 1

    if event.kind == "investigation":
        assert isinstance(event.data, InvestigationData)
        from modok.webhook.server import _process_investigation

        return asyncio.run(_process_investigation(event, quine_client))

    if event.kind == "milestone":
        assert isinstance(event.data, MilestoneData)
        from modok.webhook.server import _process_milestone

        return asyncio.run(_process_milestone(event, quine_client))

    if event.kind == "file_escalation":
        assert isinstance(event.data, FileEscalationData)
        from modok.webhook.server import _process_file_escalation

        return asyncio.run(
            _process_file_escalation(
                quine_client, event.data.project_slug, event.data.file_path, event.data.since_commit
            )
        )

    if event.kind == "root_cause_escalation":
        assert isinstance(event.data, RootCauseEscalationData)
        from modok.webhook.server import _process_root_cause_escalation

        return asyncio.run(
            _process_root_cause_escalation(
                quine_client, event.data.project_slug, event.data.feature_slug
            )
        )

    return 0
