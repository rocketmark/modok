# GitHub Ingestion

## Context and Design Philosophy

`modok ingest-github` pulls issues and pull requests from the GitHub API and writes them to Quine as `CustomerIssue` and `Fix` nodes. It is the complement to `ingest-git`: `ingest-git` writes commit and file-touch data from the local repo; `ingest-github` writes issue tracker and PR data from the GitHub API. The two commands share the `Commit` node as a bridge — a PR's `merge_commit_sha` is the same SHA that `ingest-git` already wrote, so `Fix -[:IMPLEMENTED_IN]-> Commit` edges can be written without any duplication.

No LLM is involved. All data is mechanical: GitHub's API returns structured fields that map directly to node properties. The write path is idempotent — re-running produces the same graph.

## Data Model

### Node mapping

| GitHub object | Graph node | Conditions |
|---|---|---|
| Issue | `CustomerIssue` | All states (open + closed) |
| Merged PR | `Fix` | Merged only; unmerged PRs are skipped |

### Edge mapping

| Relationship | Edge | Condition |
|---|---|---|
| Merged PR → merge commit | `Fix -[:IMPLEMENTED_IN]-> Commit` | Only if Commit node exists in graph |
| Closed issue ← PR that closed it | `CustomerIssue -[:RESOLVED_BY]-> Fix` | Only if PR is merged |

### Field mapping — CustomerIssue

| Graph field | GitHub source |
|---|---|
| `ticket_id` | `str(issue.number)` |
| `source_system` | `"github"` |
| `summary` | `issue.title` |
| `raw_text` | `issue.body` (may be null → empty string) |
| `status` | `"open"` or `"closed"` |

### Field mapping — Fix

| Graph field | GitHub source |
|---|---|
| `fix_id` | `"gh-" + str(pr.number)` (e.g. `"gh-42"`) |
| `summary` | `pr.title` |
| `kind` | `"dependency-update"` if Dependabot PR, else `"pull-request"` |

A PR is classified as Dependabot when `pr.user.login == "dependabot[bot]"`.

## Configuration

Two fields are added to each `[[projects]]` block in `~/.modok/config.toml`:

```toml
[[projects]]
slug = "stagehand"
repo = "/Users/markstalzer/github/stagehand"
github_repo = "rocketmark/stagehand"   # owner/repo
last_github_sync = ""                  # ISO 8601 timestamp; empty = never synced
```

The GitHub token is read from the `GITHUB_TOKEN` environment variable. It is never stored in the config file.

## Incremental Sync

- Without `--full`: fetches only issues and PRs with `updated_at` after `last_github_sync`. If `last_github_sync` is empty, fetches all.
- With `--full`: fetches all issues and PRs regardless of `last_github_sync`.
- On successful completion, `last_github_sync` is updated to the UTC timestamp at the start of the run (not the end, so no events are missed during a long-running sync).

## Closing Reference Detection

When a merged PR closes an issue, the `CustomerIssue -[:RESOLVED_BY]-> Fix` edge is written. Closing references are detected by parsing the PR body for GitHub's canonical closing keywords:

```
closes #N   fixes #N   resolves #N
close #N    fix #N     resolve #N
```

Matches are case-insensitive. Cross-repo references (`owner/repo#N`) are ignored — only same-repo issue numbers are followed.

If GitHub's API returns closing issues references directly (available on newer API versions), those are used instead of body parsing.

## API Access Pattern

All requests use the GitHub REST API v3 via `httpx` (async). The token is passed as `Authorization: Bearer <token>`.

| Data | Endpoint | Pagination |
|---|---|---|
| Issues | `GET /repos/{owner}/{repo}/issues?state=all&since=<ts>&per_page=100` | Cursor via `Link` header |
| PRs | `GET /repos/{owner}/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=100` | Paginate until `updated_at ≤ last_github_sync` |

Note: GitHub's `/issues` endpoint returns both issues and PRs. PRs in the issue list are skipped (identified by presence of `pull_request` key) — they are fetched separately via `/pulls`.

The incremental `since` filter on `/issues` applies to `updated_at`, not `created_at`. This ensures edits, label changes, and late closures are captured on re-sync.

## Rate Limiting

Authenticated requests have a 5,000 request/hour limit. For most projects this is not a concern — a full sync of 1,000 issues + 500 PRs costs ~16 paginated requests. If a 429 response is received, the command waits for the `Retry-After` header duration and retries once. On a second 429, it exits `2`.

## Write Order

1. `CustomerIssue` nodes (issues)
2. `Fix` nodes (merged PRs)
3. `Fix -[:IMPLEMENTED_IN]-> Commit` edges (merge commit link — silently skipped if Commit absent)
4. `CustomerIssue -[:RESOLVED_BY]-> Fix` edges (closing references)

## Module Layout

```
src/modok/ingestion/
    github.py          # GithubIngester class + ingest_github() entry point
src/modok/cli/commands/
    ingest_github.py   # modok ingest-github command
```

## Error Handling

| Condition | Behavior |
|---|---|
| `github_repo` missing from config | Exit `1`: "github_repo not set for project `<slug>` — add `github_repo = owner/repo` to config" |
| `GITHUB_TOKEN` not set | Exit `1`: "GITHUB_TOKEN environment variable not set" |
| HTTP 401/403 | Exit `1`: "GitHub token rejected or missing repo access" |
| HTTP 429 (rate limit) | Retry once after `Retry-After`; exit `2` on second 429 |
| HTTP 5xx | Exit `2`: "GitHub API unavailable" |
| Quine unreachable | Exit `2` (standard ping check on startup) |
| Commit node absent for IMPLEMENTED_IN | Silently skip edge (matches TOUCHES behavior in ingest-git) |

## Decisions and Alternatives

| Decision | Chosen | Alternative | Rationale |
|---|---|---|---|
| Token source | `GITHUB_TOKEN` env var | Config file | Tokens are secrets; config files are often committed or shared |
| PR → Fix condition | Merged only | All closed | Closed-unmerged PRs were rejected; including them would add noise Fix nodes with no commit link |
| Closing references | Body parsing + API | API only | GitHub's `closing_issues_references` field isn't available on all API versions; body parsing is a reliable fallback |
| Dependabot detection | `user.login == "dependabot[bot]"` | Label-based | Login is stable; labels vary per repo |
| HTTP client | `httpx` (async) | `requests` (sync) | Consistent with the rest of modok's async pattern |

## Open Questions

1. Should `Fix -[:IMPLEMENTED_IN]-> Commit` be traversed by the DRE to surface the specific fix commit in the debug packet? Deferred — `pr_url` already links to the PR (which shows the merge commit), and adding a Fix→Commit traversal expands the DRE's scope. Revisit when there is a concrete use case for surfacing fix commit SHAs directly.
