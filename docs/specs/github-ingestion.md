# GitHub Ingestion Specs

Specs for `modok ingest-github` — pulls GitHub issues and merged PRs and writes `CustomerIssue`, `Fix`, and resolution edges to Quine.

LLD: `docs/llds/github-ingestion.md`

---

## Test Level Convention

- **[U]** — Unit test with mocked GitHub API and mocked Quine client.
- **[I]** — Integration test against live GitHub API (requires `GITHUB_TOKEN`; marked slow).
- **[C]** — Contract test against live Quine instance.

---

## Config and Auth

- [x] **GHING-CONF-001** [U]: WHEN `ingest-github` is invoked and `github_repo` is absent from the project config, THE SYSTEM SHALL exit 1 with the message "github_repo not set for project `<slug>` — add `github_repo = owner/repo` to config".

- [x] **GHING-CONF-002** [U]: WHEN `ingest-github` is invoked and `GITHUB_TOKEN` is not set in the environment, THE SYSTEM SHALL exit 1 with the message "GITHUB_TOKEN environment variable not set".

- [x] **GHING-CONF-003** [U]: WHEN a sync completes successfully, THE SYSTEM SHALL write `last_github_sync` to the project config as a UTC ISO 8601 timestamp recorded at the start of the run, not the end.

- [x] **GHING-AUTH-001** [U]: THE SYSTEM SHALL authenticate all GitHub API requests with `Authorization: Bearer <token>`.

- [x] **GHING-AUTH-002** [U]: IF the GitHub API returns HTTP 401 or 403, THE SYSTEM SHALL exit 1 with the message "GitHub token rejected or missing repo access".

---

## Incremental Sync

- [x] **GHING-SYNC-001** [U]: WHEN `--full` is not set AND `last_github_sync` is non-empty, THE SYSTEM SHALL fetch only issues and PRs with `updated_at` after `last_github_sync`. For issues, this is passed as the `since` query parameter. For PRs (which lack a `since` parameter), the system SHALL paginate `?state=closed&sort=updated&direction=desc` and stop fetching when it encounters a page where all items have `updated_at ≤ last_github_sync`.

- [x] **GHING-SYNC-002** [U]: WHEN `--full` is set OR `last_github_sync` is empty, THE SYSTEM SHALL fetch all issues and PRs regardless of `updated_at`.

- [x] **GHING-SYNC-003** [U]: `--full` SHALL take precedence over `last_github_sync`; if both are present, the system behaves as if `last_github_sync` is empty.

---

## Issue Ingestion

- [x] **GHING-ISSUE-001** [U]: FOR EACH GitHub issue returned by the API that does not have a `pull_request` key, THE SYSTEM SHALL upsert a `CustomerIssue` node with:
  - `source_system = "github"`
  - `ticket_id = str(issue.number)`
  - `summary = issue.title`
  - `raw_text = issue.body` (empty string if null)
  - `status = "open"` if `issue.state == "open"`, else `"closed"`
  - `project_slug` = project slug from config

- [x] **GHING-ISSUE-002** [U]: GitHub objects with a `pull_request` key SHALL be skipped during issue ingestion; they are handled in PR ingestion.
- [x] **GHING-ISSUE-003** [U]: THE SYSTEM SHALL derive `ticket_kind` from `issue.labels` (a list of label objects, each with a `name` string) via `ticket_kind_from_labels`: a case-insensitive substring match of `"bug"` in any label name sets `ticket_kind = "bug"`; a case-insensitive substring match of `"feature"` or `"enhancement"` sets `ticket_kind = "feature_request"`; `"bug"` takes precedence if a label somehow matches both. If no label matches either, or `issue.labels` is absent/empty, `ticket_kind` SHALL be `None`. This is a mechanical, structured-input classification — the label is explicit metadata the reporter (or an issue template) already assigned, not inferred from free text.

---

## PR Ingestion

- [x] **GHING-PR-001** [U]: FOR EACH merged GitHub PR (where `pr.merged_at` is non-null), THE SYSTEM SHALL upsert a `Fix` node with:
  - `fix_id = "gh-" + str(pr.number)` (e.g. `"gh-42"`)
  - `summary = pr.title`
  - `kind = "dependency-update"` if `pr.user.login == "dependabot[bot]"`, else `"pull-request"`
  - `pr_url = pr.html_url`
  - `project_slug` = project slug from config

- [x] **GHING-PR-002** [U]: PRs that are closed but not merged (`merged_at` is null) SHALL be skipped.

- [x] **GHING-PR-003** [U]: Open non-Dependabot PRs SHALL be skipped.

- [x] **GHING-PR-004** [U]: Merged Dependabot PRs SHALL produce a `Fix` node only. No `CustomerIssue` node is written for a merged Dependabot PR.

- [x] **GHING-PR-005** [U]: Open Dependabot PRs SHALL produce a `CustomerIssue` node with `source_system="github"`, `ticket_id=str(pr.number)`, `summary=pr.title`, `raw_text=pr.body or ""`, and `status="open"`. No `Fix` node is written for an open Dependabot PR.

---

## Resolution Edges

- [x] **GHING-RES-001** [U]: FOR EACH merged PR, THE SYSTEM SHALL write a `Fix -[:IMPLEMENTED_IN]-> Commit` edge using `pr.merge_commit_sha`. IF no Commit node with that SHA exists in the graph, the edge SHALL be silently skipped. This existence check SHALL use `node_exists_by_parts` (`docs/specs/quine-client.md § QC-NR-004`), never a Python-computed `idFrom()` value — found live to otherwise always report "absent" regardless of whether the Commit node actually exists.

- [x] **GHING-RES-002** [U]: THE SYSTEM SHALL detect closing references by scanning the PR body for the pattern `(closes?|fixes?|resolves?)\s+#(\d+)` (case-insensitive, all matches).

- [x] **GHING-RES-003** [U]: FOR EACH closing issue number detected, IF the corresponding `CustomerIssue` node exists in the graph, THE SYSTEM SHALL write a `CustomerIssue -[:RESOLVED_BY]-> Fix` edge. IF the `CustomerIssue` node does not exist, the edge SHALL be silently skipped. Same `node_exists_by_parts` requirement as GHING-RES-001.

- [x] **GHING-RES-004** [U]: Cross-repo closing references (e.g. `owner/repo#N`) SHALL be ignored; only bare `#N` references are followed.

---

## Rate Limiting

- [x] **GHING-RATE-001** [U]: IF the GitHub API returns HTTP 429, THE SYSTEM SHALL wait for the duration specified in the `Retry-After` response header (or 60 seconds if the header is absent), then retry the request once.

- [x] **GHING-RATE-002** [U]: IF the retry after a 429 also returns 429, THE SYSTEM SHALL exit 2 with "GitHub API rate limit exceeded — retry after <n> seconds".

---

## Error Handling

- [x] **GHING-ERR-001** [U]: IF Quine is unreachable at startup, THE SYSTEM SHALL exit 2 with the standard unreachable message (consistent with all other graph-touching commands).

- [x] **GHING-ERR-002** [U]: IF the GitHub API returns HTTP 5xx, THE SYSTEM SHALL exit 2 with "GitHub API unavailable".

---

## Deleted Ticket Detection

- [ ] **GHING-DEL-001** [U]: `reconcile_deleted_tickets(client, project_slug, github_repo, token)` SHALL fetch every currently-visible issue number from GitHub via full, paginated `GET /repos/{owner}/{repo}/issues?state=all` with **no** `since` parameter, regardless of any incremental sync cursor.
- [ ] **GHING-DEL-002** [U]: PR-flavored entries (identified by the `pull_request` key) SHALL NOT be filtered out of the fetched number set — unlike `ingest_issue`'s skip rule, this check needs every currently-existing number, including open Dependabot PRs tracked as `CustomerIssue`.
- [ ] **GHING-DEL-003** [U]: THE SYSTEM SHALL query the graph for every `CustomerIssue` in the project WHERE `source_system == "github"` AND `status != "deleted"`.
- [ ] **GHING-DEL-004** [U, P]: A queried `CustomerIssue` whose `ticket_id` is absent from the fetched number set SHALL have its `status` set to `"deleted"` via a targeted property `SET` (not a full `upsert_node` rewrite of every field).
- [ ] **GHING-DEL-005** [U]: A `CustomerIssue` whose `ticket_id` IS present in the fetched number set SHALL NOT be modified, regardless of its current `status` value.
- [ ] **GHING-DEL-006** [U]: A `CustomerIssue` with `source_system != "github"` SHALL NOT be considered by this reconciliation at all — it never appears in GHING-DEL-003's query.
- [ ] **GHING-DEL-007** [U]: A `CustomerIssue` already marked `status == "deleted"` SHALL be excluded from GHING-DEL-003's query on every subsequent run — it is never re-checked or un-marked.
- [ ] **GHING-DEL-008** [U]: WHEN the full-list fetch (GHING-DEL-001) fails (network error, non-2xx response, exception), THE SYSTEM SHALL log the failure, mark no `CustomerIssue` as deleted this cycle, and SHALL NOT raise.
- [ ] **GHING-DEL-009** [U]: `reconcile_deleted_tickets` SHALL be called once per poll cycle, per GitHub-configured project, isolated in its own `try`/`except` in `_poll_once` (`src/modok/webhook/adapters/github_poll.py`) — a failure SHALL NOT block issue/PR sync, CI ingestion, dependency ingestion, or either escalation pattern's reconciliation in the same cycle.
- [ ] **GHING-DEL-010** [U]: WHEN `github_repo` is unconfigured or `GITHUB_TOKEN` is unset for a project, `reconcile_deleted_tickets` SHALL NOT be called for that project (mirrors the existing gate already applied to issue/PR sync itself).

---

## Data Model

- [x] **GHING-MODEL-001** [U]: The `Fix` model SHALL include a `pr_url: str | None = None` field.
- [x] **GHING-MODEL-002** [U]: The `CustomerIssue` model SHALL include a `ticket_kind: str | None = None` field (GHING-ISSUE-003).

---

## GitHub Event Routing Unification

See `docs/llds/continuous-ci-ingestion.md § Prerequisite: Unified GitHub Event Routing` for the full rationale (three independently-found pre-existing inconsistencies between the poll/batch path and the webhook path). Specs below describe the *target* state after unification; GHING-ISSUE-001 through GHING-RES-004 above describe the resulting graph writes, which are unchanged in shape — only which code path produces them changes.

- [ ] **GHING-ROUTE-001** [U, C]: `GithubIngester.ingest_issue` shall build a `CustomerIssueData` from the raw polled issue dict and dispatch it via `run_ingest_event`, rather than upserting the `CustomerIssue` node and calling anchor linking inline. Its public signature (`issue: dict -> bool`) and all current callers (`GithubIngester.run()`, the poll adapter, `modok ingest-github`) shall not change.
- [ ] **GHING-ROUTE-002** [U, C]: `GithubIngester.ingest_pr` shall build a `FixData` (or, for an open Dependabot PR, a `CustomerIssueData`) from the raw polled PR dict and dispatch it via `run_ingest_event`, rather than upserting nodes and writing edges inline. Its public signature (`pr: dict -> bool`) shall not change.
- [ ] **GHING-ROUTE-003** [U]: `FixData` shall gain `pr_url: str | None`, `merge_commit_sha: str | None`, `closing_issue_numbers: list[str]`, and `is_open_dependabot: bool` fields, populated identically by the webhook envelope-unwrapping path (`GitHubAdapter.normalize_event`) and the raw-polled-dict path (`GithubIngester.ingest_pr`) via one shared field-mapping helper per resource type — not two independent mappings.
- [ ] **GHING-ROUTE-004** [U]: `run_ingest_event`'s `fix` branch, given `is_open_dependabot=True`, shall upsert a `CustomerIssue` node instead of a `Fix` node and return (matching GHING-PR-005's existing behavior). Otherwise it shall upsert the `Fix` node (GHING-PR-001), then write `IMPLEMENTED_IN` per GHING-RES-001's existence gate and `RESOLVED_BY` per GHING-RES-003's existence gate, using `merge_commit_sha`/`closing_issue_numbers` from the event data.
- [ ] **GHING-ROUTE-005** [U]: `GithubIngester._link_anchors` shall be removed once parity tests (below) confirm `ingest_issue`'s dispatch through `run_ingest_event` (which already performs mechanical anchor linking plus LLM fallback for the webhook path) produces identical anchor-linking behavior for poll/batch-originated `CustomerIssue` writes.
- [ ] **GHING-ROUTE-006** [U]: Before GHING-ROUTE-001 through 005 are implemented, characterization tests shall exist capturing current (pre-unification) behavior for both the poll/batch path (`GithubIngester.ingest_issue`/`ingest_pr` against representative fixtures: a plain issue, an edited issue, a merged PR with closing references, an open Dependabot PR) and the webhook path (`GitHubAdapter.normalize_event` → `run_ingest_event` against the equivalent webhook payload shapes). These tests shall pass against the pre-unification implementation before any routing code changes.
- [ ] **GHING-ROUTE-007** [P]: After the routing change, the fixtures from GHING-ROUTE-006 shall produce identical graph state (node fields, edges, anchor-linking results) through both the poll/batch path and the webhook path — except that the webhook path shall now also produce `IMPLEMENTED_IN`/`RESOLVED_BY` edges and dependabot handling for merged/open PRs, which it did not produce before unification (an intentional, called-out closing of a pre-existing gap, not a divergence to fix).
- [ ] **GHING-ROUTE-008** [U]: `GithubIngester.ingest_issue`/`ingest_pr` shall call `run_ingest_event` via `await asyncio.to_thread(run_ingest_event, event, self._quine)`, not a direct synchronous call — `run_ingest_event` internally calls `asyncio.run(...)`, which raises `RuntimeError` if invoked from within an already-running event loop, which `ingest_issue`/`ingest_pr` (both `async def`) always are.
