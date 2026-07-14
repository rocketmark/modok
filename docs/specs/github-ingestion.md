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

## Data Model

- [x] **GHING-MODEL-001** [U]: The `Fix` model SHALL include a `pr_url: str | None = None` field.
- [x] **GHING-MODEL-002** [U]: The `CustomerIssue` model SHALL include a `ticket_kind: str | None = None` field (GHING-ISSUE-003).
