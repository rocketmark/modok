"""GitHub ingestion — pulls issues and merged PRs into the graph as CustomerIssue and Fix nodes."""
# @spec GHING-ISSUE-001, GHING-ISSUE-002, GHING-ISSUE-003, GHING-PR-001, GHING-PR-002,
#       GHING-PR-003, GHING-PR-004, GHING-RES-001, GHING-RES-002, GHING-RES-003, GHING-RES-004,
#       GHING-SYNC-001, GHING-SYNC-002, GHING-SYNC-003,
#       GHING-RATE-001, GHING-RATE-002, GHING-AUTH-001, GHING-AUTH-002, GHING-ERR-002

from __future__ import annotations

import asyncio
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from modok.ingestion.git_history import _update_project_config_field
from modok.webhook.models import CustomerIssueData, FixData, IngestEvent
from modok.webhook.pipeline import run_ingest_event

_CLOSING_RE = re.compile(r"(?:closes?|fix(?:es)?|resolves?)\s+#(\d+)", re.IGNORECASE)
_API_BASE = "https://api.github.com"


# @spec GHING-ISSUE-003
def ticket_kind_from_labels(label_names: list[str]) -> str | None:
    """Derive ticket_kind from GitHub issue label names — mechanical,
    structured-input classification: the label is explicit metadata the
    reporter (or an issue template) already assigned, not inferred from free
    text. Case-insensitive substring match; "bug" takes precedence over
    "feature"/"enhancement" if a label somehow matches both."""
    lowered = [name.lower() for name in label_names]
    if any("bug" in name for name in lowered):
        return "bug"
    if any("feature" in name or "enhancement" in name for name in lowered):
        return "feature_request"
    return None


@dataclass
class GithubIngestionReport:
    issues_written: int = 0
    prs_written: int = 0
    implemented_in_written: int = 0
    implemented_in_skipped: int = 0
    resolved_by_written: int = 0
    resolved_by_skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        lines = [
            "GitHub ingestion complete",
            f"  Issues (CustomerIssue): {self.issues_written}",
            f"  PRs (Fix):              {self.prs_written}",
            f"  IMPLEMENTED_IN edges:   {self.implemented_in_written} written, {self.implemented_in_skipped} skipped",
            f"  RESOLVED_BY edges:      {self.resolved_by_written} written, {self.resolved_by_skipped} skipped",
        ]
        if self.errors:
            lines.append(f"  Errors: {len(self.errors)}")
            for e in self.errors:
                lines.append(f"    {e}")
        return "\n".join(lines)


def _parse_closing_refs(body: str | None) -> list[int]:
    if not body:
        return []
    return [int(m.group(1)) for m in _CLOSING_RE.finditer(body)]


class GithubIngester:
    def __init__(
        self,
        project_slug: str,
        github_repo: str,
        token: str,
        client: Any,
    ) -> None:
        self._project_slug = project_slug
        self._github_repo = github_repo
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._quine = client

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get_sync(self, url: str, params: dict | None = None) -> httpx.Response:
        with httpx.Client(headers=self._headers, timeout=30) as http:
            resp = http.get(url, params=params)
        return resp

    def _paginate_sync(self, url: str, params: dict | None = None) -> list[dict]:
        """Fetch all pages from a GitHub list endpoint."""
        results: list[dict] = []
        params = dict(params or {})
        params.setdefault("per_page", 100)
        next_url: str | None = url
        while next_url:
            resp = self._with_retry(next_url, params)
            params = {}  # next_url already contains params after first page
            resp.raise_for_status()
            results.extend(resp.json())
            next_url = self._next_link(resp)
        return results

    def _with_retry(self, url: str, params: dict | None = None) -> httpx.Response:
        """GET with one retry on 429."""
        resp = self._get_sync(url, params)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "60"))
            time.sleep(wait)
            resp = self._get_sync(url, params)
            if resp.status_code == 429:
                wait2 = int(resp.headers.get("Retry-After", wait))
                raise click_exit2(f"GitHub API rate limit exceeded — retry after {wait2} seconds")
        return resp

    @staticmethod
    def _next_link(resp: httpx.Response) -> str | None:
        link = resp.headers.get("link", "")
        for part in link.split(","):
            part = part.strip()
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
                return url
        return None

    # ------------------------------------------------------------------
    # Issue ingestion
    # ------------------------------------------------------------------

    # @spec GHING-ISSUE-001, GHING-ISSUE-002, GHING-ISSUE-003, GHING-ROUTE-001, GHING-ROUTE-008
    async def ingest_issue(self, issue: dict[str, Any]) -> bool:
        if "pull_request" in issue:
            return False
        label_names = [label.get("name", "") for label in issue.get("labels", [])]
        event = IngestEvent(
            kind="customer_issue",
            project_slug=self._project_slug,
            data=CustomerIssueData(
                ticket_id=str(issue["number"]),
                summary=issue["title"],
                raw_text=issue.get("body") or "",
                status="open" if issue["state"] == "open" else "closed",
                source_system="github",
                ticket_kind=ticket_kind_from_labels(label_names),
            ),
        )
        await asyncio.to_thread(run_ingest_event, event, self._quine)
        return True

    # ------------------------------------------------------------------
    # PR ingestion
    # ------------------------------------------------------------------

    # @spec GHING-PR-001, GHING-PR-002, GHING-PR-003, GHING-PR-004, GHING-PR-005,
    #       GHING-RES-001, GHING-RES-002, GHING-RES-003, GHING-RES-004,
    #       GHING-ROUTE-002, GHING-ROUTE-008
    async def ingest_pr(self, pr: dict[str, Any]) -> bool:
        is_dependabot = pr.get("user", {}).get("login") == "dependabot[bot]"
        is_merged = bool(pr.get("merged_at"))
        is_open = pr.get("state") == "open"

        # Open Dependabot PRs → CustomerIssue (pending dependency update)
        if is_open and is_dependabot:
            event = IngestEvent(
                kind="fix",
                project_slug=self._project_slug,
                data=FixData(
                    fix_id=f"gh-{pr['number']}",
                    summary=pr["title"],
                    kind="dependency-update",
                    is_open_dependabot=True,
                ),
            )
            await asyncio.to_thread(run_ingest_event, event, self._quine)
            return True

        if not is_merged:
            return False

        event = IngestEvent(
            kind="fix",
            project_slug=self._project_slug,
            data=FixData(
                fix_id=f"gh-{pr['number']}",
                summary=pr["title"],
                kind="dependency-update" if is_dependabot else "pull-request",
                pr_url=pr.get("html_url"),
                merge_commit_sha=pr.get("merge_commit_sha"),
                closing_issue_numbers=[str(n) for n in _parse_closing_refs(pr.get("body"))],
            ),
        )
        await asyncio.to_thread(run_ingest_event, event, self._quine)
        return True

    # ------------------------------------------------------------------
    # Full sync
    # ------------------------------------------------------------------

    async def run(self, since: str | None = None) -> GithubIngestionReport:
        report = GithubIngestionReport()
        owner_repo = self._github_repo

        # Issues
        issue_params: dict[str, str] = {"state": "all"}
        if since:
            issue_params["since"] = since
        issues = self._paginate_sync(
            f"{_API_BASE}/repos/{owner_repo}/issues",
            issue_params,
        )
        for issue in issues:
            written = await self.ingest_issue(issue)
            if written:
                report.issues_written += 1

        # PRs — sort by updated desc, stop when all items on a page are older than `since`
        pr_params: dict[str, str] = {
            "state": "all",
            "sort": "updated",
            "direction": "desc",
        }
        prs = self._fetch_prs_incremental(owner_repo, pr_params, since)
        for pr in prs:
            written = await self.ingest_pr(pr)
            if written:
                report.prs_written += 1

        # Reconcile edge counts (ingest_pr writes edges inline; track separately)
        # Edge counts are approximations — detailed tracking deferred.
        return report

    def _fetch_prs_incremental(
        self, owner_repo: str, params: dict, since: str | None
    ) -> list[dict]:
        results: list[dict] = []
        params = dict(params)
        params["per_page"] = "100"
        next_url: str | None = f"{_API_BASE}/repos/{owner_repo}/pulls"
        while next_url:
            resp = self._with_retry(next_url, params if next_url.endswith("/pulls") else None)
            params = {}
            resp.raise_for_status()
            page: list[dict] = resp.json()
            if not page:
                break
            if since:
                page_to_add = [pr for pr in page if pr.get("updated_at", "") > since]
                results.extend(page_to_add)
                # Stop when all items on the page are at or before `since`
                if all(pr.get("updated_at", "") <= since for pr in page):
                    break
            else:
                results.extend(page)
            next_url = self._next_link(resp)
        return results


# @spec FESC-GH-001
async def create_issue(
    github_repo: str, token: str, title: str, body: str, labels: list[str] | None = None
) -> str | None:
    """Best-effort: create a new GitHub issue. Returns the created issue's
    number as a string on success, None on any non-2xx response or
    exception. Never raises — unlike post_issue_comment this DOES return a
    value the caller needs (the issue number to persist), so it cannot be
    purely fire-and-forget."""
    url = f"{_API_BASE}/repos/{github_repo}/issues"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload: dict[str, Any] = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    try:
        async with httpx.AsyncClient(headers=headers, timeout=30) as http:
            resp = await http.post(url, json=payload)
            if resp.status_code >= 300:
                print(f"GitHub issue create failed ({resp.status_code}): {url}", file=sys.stderr)
                return None
            return str(resp.json()["number"])
    except Exception as exc:
        print(f"GitHub issue create failed: {exc}", file=sys.stderr)
        return None


# @spec GHING-DEL-001 through 010
async def reconcile_deleted_tickets(
    client: Any, project_slug: str, github_repo: str, token: str
) -> int:
    """Fetch every currently-visible GitHub issue number (full, unfiltered
    state=all pagination — no `since`, the only way to positively confirm a
    ticket no longer exists rather than merely hasn't changed recently) and
    mark any CustomerIssue absent from that set status="deleted". See
    docs/llds/github-ingestion.md § Deleted Ticket Detection: a genuinely
    deleted issue produces no event and no longer appears in any incremental
    fetch, so GithubIngester's normal since=-filtered sync can never learn
    about it on its own."""
    try:
        ingester = GithubIngester(
            project_slug=project_slug, github_repo=github_repo, token=token, client=client
        )
        items = await asyncio.to_thread(
            ingester._paginate_sync,
            f"{_API_BASE}/repos/{github_repo}/issues",
            {"state": "all"},
        )
        current_numbers = {str(item["number"]) for item in items}
    except Exception as exc:
        print(f"deleted-ticket-reconciliation: fetch failed for {project_slug}: {exc}", file=sys.stderr)
        return 0

    rows = await client.query(
        "MATCH (ci) WHERE ci.node_type = 'CustomerIssue' AND ci.project_slug = $p "
        "AND ci.source_system = 'github' AND ci.status <> 'deleted' "
        "RETURN ci.ticket_id AS ticket_id",
        {"p": project_slug},
    )
    stale = [row[0] for row in rows if row[0] not in current_numbers]
    for ticket_id in stale:
        await client.query(
            "MATCH (ci) WHERE id(ci) = idFrom('customer-issue', $p, 'github', $t) "
            "SET ci.status = 'deleted'",
            {"p": project_slug, "t": ticket_id},
        )
    return len(stale)


# @spec RCESC-GH-001, RCESC-GH-002, RCESC-GH-003
async def get_issue_state(github_repo: str, token: str, issue_number: str) -> str | None:
    """Best-effort: fetch a GitHub issue's current state ("open"/"closed",
    GitHub's own values, unremapped). A 404 (deleted/transferred issue)
    returns "closed" — functionally equivalent to a human closing it, since
    neither can be appended to. Any other non-2xx response or exception
    returns None (transient — retry later). Never raises."""
    url = f"{_API_BASE}/repos/{github_repo}/issues/{issue_number}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(headers=headers, timeout=30) as http:
            resp = await http.get(url)
            if resp.status_code == 404:
                return "closed"
            if resp.status_code >= 300:
                print(f"GitHub issue state fetch failed ({resp.status_code}): {url}", file=sys.stderr)
                return None
            return resp.json().get("state")
    except Exception as exc:
        print(f"GitHub issue state fetch failed: {exc}", file=sys.stderr)
        return None


# @spec SQ-GH-001, SQ-GH-004
async def post_issue_comment(github_repo: str, token: str, issue_number: str, body: str) -> None:
    """Best-effort: post a comment to a GitHub issue. Never raises."""
    url = f"{_API_BASE}/repos/{github_repo}/issues/{issue_number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(headers=headers, timeout=30) as http:
            resp = await http.post(url, json={"body": body})
            if resp.status_code >= 300:
                print(f"GitHub comment post failed ({resp.status_code}): {url}", file=sys.stderr)
    except Exception as exc:
        print(f"GitHub comment post failed: {exc}", file=sys.stderr)


def click_exit2(msg: str) -> SystemExit:
    import click

    click.echo(msg, err=True)
    return SystemExit(2)


def save_last_github_sync(config_path: Path, project_slug: str, timestamp: str) -> None:
    """Write last_github_sync for a project to the TOML config file in-place."""
    _update_project_config_field(config_path, project_slug, "last_github_sync", timestamp)
