"""GitHub ingestion — pulls issues and merged PRs into the graph as CustomerIssue and Fix nodes."""
# @spec GHING-ISSUE-001, GHING-ISSUE-002, GHING-PR-001, GHING-PR-002, GHING-PR-003,
#       GHING-PR-004, GHING-RES-001, GHING-RES-002, GHING-RES-003, GHING-RES-004,
#       GHING-SYNC-001, GHING-SYNC-002, GHING-SYNC-003,
#       GHING-RATE-001, GHING-RATE-002, GHING-AUTH-001, GHING-AUTH-002, GHING-ERR-002

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from modok.ingestion.git_history import _update_project_config_field
from modok.quine.ids import idFrom as _idFrom
from modok.quine.models import CustomerIssue, Fix

_CLOSING_RE = re.compile(r"(?:closes?|fix(?:es)?|resolves?)\s+#(\d+)", re.IGNORECASE)
_API_BASE = "https://api.github.com"


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

    # @spec GHING-ISSUE-001, GHING-ISSUE-002
    async def ingest_issue(self, issue: dict[str, Any]) -> bool:
        if "pull_request" in issue:
            return False
        node = CustomerIssue(
            node_type="CustomerIssue",
            project_slug=self._project_slug,
            source_system="github",
            ticket_id=str(issue["number"]),
            summary=issue["title"],
            raw_text=issue.get("body") or "",
            status="open" if issue["state"] == "open" else "closed",
        )
        await self._quine.upsert_node(node)
        return True

    # ------------------------------------------------------------------
    # PR ingestion
    # ------------------------------------------------------------------

    # @spec GHING-PR-001, GHING-PR-002, GHING-PR-003, GHING-PR-004, GHING-PR-005,
    #       GHING-RES-001, GHING-RES-002, GHING-RES-003, GHING-RES-004
    async def ingest_pr(self, pr: dict[str, Any]) -> bool:
        is_dependabot = pr.get("user", {}).get("login") == "dependabot[bot]"
        is_merged = bool(pr.get("merged_at"))
        is_open = pr.get("state") == "open"

        # Open Dependabot PRs → CustomerIssue (pending dependency update)
        if is_open and is_dependabot:
            node = CustomerIssue(
                node_type="CustomerIssue",
                project_slug=self._project_slug,
                source_system="github",
                ticket_id=str(pr["number"]),
                summary=pr["title"],
                raw_text=pr.get("body") or "",
                status="open",
            )
            await self._quine.upsert_node(node)
            return True

        if not is_merged:
            return False

        node = Fix(
            node_type="Fix",
            project_slug=self._project_slug,
            fix_id=f"gh-{pr['number']}",
            summary=pr["title"],
            kind="dependency-update" if is_dependabot else "pull-request",
            pr_url=pr.get("html_url"),
        )
        await self._quine.upsert_node(node)

        # IMPLEMENTED_IN edge
        merge_sha = pr.get("merge_commit_sha")
        if merge_sha:
            commit_id = _idFrom("commit", self._project_slug, merge_sha)
            if await self._quine.node_exists(commit_id):
                await self._quine.write_edge_by_parts(
                    ("fix", self._project_slug, f"gh-{pr['number']}"),
                    "IMPLEMENTED_IN",
                    ("commit", self._project_slug, merge_sha),
                )

        # RESOLVED_BY edges from closing references
        for issue_num in _parse_closing_refs(pr.get("body")):
            ci_id = _idFrom("customer-issue", self._project_slug, "github", str(issue_num))
            if await self._quine.node_exists(ci_id):
                await self._quine.write_edge_by_parts(
                    ("customer-issue", self._project_slug, "github", str(issue_num)),
                    "RESOLVED_BY",
                    ("fix", self._project_slug, f"gh-{pr['number']}"),
                )

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


def click_exit2(msg: str) -> SystemExit:
    import click
    click.echo(msg, err=True)
    return SystemExit(2)


def save_last_github_sync(config_path: Path, project_slug: str, timestamp: str) -> None:
    """Write last_github_sync for a project to the TOML config file in-place."""
    _update_project_config_field(config_path, project_slug, "last_github_sync", timestamp)
