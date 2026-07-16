# GitHub Ingestion

## Context and Design Philosophy

`modok ingest-github` pulls issues and pull requests from the GitHub API and writes them to Quine as `CustomerIssue` and `Fix` nodes. It is the complement to `ingest-git`: `ingest-git` writes commit and file-touch data from the local repo; `ingest-github` writes issue tracker and PR data from the GitHub API. The two commands share the `Commit` node as a bridge — a PR's `merge_commit_sha` is the same SHA that `ingest-git` already wrote, so `Fix -[:IMPLEMENTED_IN]-> Commit` edges can be written without any duplication.

No LLM is involved. All data is mechanical: GitHub's API returns structured fields that map directly to node properties. The write path is idempotent — re-running produces the same graph.

**`ingest_issue`/`ingest_pr` are normalize-and-dispatch wrappers, not the mutation owners.** As of `docs/llds/continuous-ci-ingestion.md § Prerequisite: Unified GitHub Event Routing`, both methods build a normalized `IngestEvent` (`CustomerIssueData`/`FixData`) from the raw GitHub API dict and call `run_ingest_event` (`src/modok/webhook/server.py`) — the same function the webhook push path already used — rather than upserting nodes and writing edges inline. Their public signatures (`issue: dict -> bool`, `pr: dict -> bool`) and all three current callers (`GithubIngester.run()`, the GitHub poll adapter, `modok ingest-github`) are unchanged; only the internals moved. This is why the sections below describe *what gets written*, not *which function writes it* — the mutations described here now happen inside `run_ingest_event`, shared with the webhook path, rather than being duplicated in this module.

## Data Model

### Node mapping

| GitHub object | Graph node | Conditions |
|---|---|---|
| Issue | `CustomerIssue` | All states (open + closed) |
| Merged PR | `Fix` | Merged only; unmerged, non-Dependabot PRs are skipped |
| Open Dependabot PR | `CustomerIssue` | `state == "open"` and `user.login == "dependabot[bot]"` — treated as a pending dependency-update ticket, not a `Fix`, until merged. This row was previously undocumented here despite being live in `ingest_pr` — added as part of bringing this file's data model back in sync with the code before extending it (`docs/llds/continuous-ci-ingestion.md`). |

### Edge mapping

| Relationship | Edge | Condition |
|---|---|---|
| Merged PR → merge commit | `Fix -[:IMPLEMENTED_IN]-> Commit` | Only if Commit node exists in graph |
| Closed issue ← PR that closed it | `CustomerIssue -[:RESOLVED_BY]-> Fix` | Only if PR is merged |

These two edges are now written by `run_ingest_event`'s `fix` branch (extended for this purpose — see `docs/llds/continuous-ci-ingestion.md`), not inline in `ingest_pr`. `FixData` (`src/modok/webhook/models.py`) carries the fields needed to write them: `merge_commit_sha`, `closing_issue_numbers`, `pr_url`, `is_open_dependabot`.

### Field mapping — CustomerIssue

| Graph field | GitHub source |
|---|---|
| `ticket_id` | `str(issue.number)` |
| `source_system` | `"github"` |
| `summary` | `issue.title` |
| `raw_text` | `issue.body` (may be null → empty string) |
| `status` | `"open"` or `"closed"` |
| `ticket_kind` | Derived from `issue.labels` — see § Ticket Kind from Labels |

## Ticket Kind from Labels

`ticket_kind_from_labels(label_names: list[str]) -> str | None` (`src/modok/ingestion/github.py`) derives whether a ticket is a bug report or a feature request from the reporter's own GitHub labels — explicit, structured metadata, not a text classifier. Case-insensitive substring match: any label containing `"bug"` → `"bug"`; any label containing `"feature"` or `"enhancement"` → `"feature_request"` (covers GitHub's own default `"enhancement"` label with no configuration needed); `"bug"` wins if a label somehow matches both; no match (or no labels at all) → `None`.

This is deliberately **not** the mechanical/LLM anchor classification path (`docs/llds/standing-queries.md § LLM Fallback Anchor Classification`), which was scoped to error/feature anchors only and explicitly dropped a text-based `ticket_kind` classifier as too unreliable (sentence-*shape* heuristics misfire far more than the entity-name matching anchor linking does). Labels sidestep that problem entirely: the classification is made once, explicitly, by whoever files the ticket — commonly enforced via a GitHub issue template (`.github/ISSUE_TEMPLATE/*.yml`) that requires selecting "Bug" or "Feature request" and auto-applies the corresponding label, so `ticket_kind` is populated for every new ticket without relying on wording at all.

Called from both `CustomerIssue`-writing paths so a ticket's `ticket_kind` is identical regardless of arrival path:

- `GithubIngester.ingest_issue` (batch `ingest-github` and the poll adapter) — reads `issue.get("labels", [])`, each a `{"name": str, ...}` object per GitHub's REST/webhook schema.
- `GitHubAdapter.normalize_event` (webhook push, `docs/llds/webhook-receiver.md § GitHub Adapter`) — same extraction from the webhook payload's `issue.labels`.

`run_ingest_event`'s `customer_issue` branch (`src/modok/webhook/server.py`) passes `event.data.ticket_kind` straight through to the `CustomerIssue` node — no additional logic there.

Because GitHub bumps an issue's `updated_at` when its labels change (the `"labeled"` webhook action is already in `_ISSUE_ACTIONS`, and the poll adapter's `since` incremental fetch keys off `updated_at`), labeling an issue after creation — not just via a template at creation time — re-triggers ingestion and populates `ticket_kind` on the next webhook delivery or poll cycle either way.

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

1. `CustomerIssue` nodes (issues, and open Dependabot PRs)
2. `Fix` nodes (merged, non-Dependabot PRs)
3. `Fix -[:IMPLEMENTED_IN]-> Commit` edges (merge commit link — silently skipped if Commit absent)
4. `CustomerIssue -[:RESOLVED_BY]-> Fix` edges (closing references)

Steps 2–4 all happen inside `run_ingest_event`'s `fix` branch now (`docs/llds/continuous-ci-ingestion.md`); `ingest_pr` only builds the `FixData` and dispatches.

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

**Node existence checks use `node_exists_by_parts`, never a Python-computed `idFrom()`.** Both the `IMPLEMENTED_IN` (Commit) and `RESOLVED_BY` (CustomerIssue) existence gates were found live to be silently broken: they computed an ID via `modok.quine.ids.idFrom()` (a SHA-256 int64, test-harness-only) and passed it to `node_exists()`, which always returned `False` against real Quine — Quine's real node IDs are UUIDs computed by its own `idFrom()` Cypher function, not this Python value. This meant `IMPLEMENTED_IN`/`RESOLVED_BY` edges never actually got written against a real Quine instance, despite passing every mocked unit test. Fixed by switching both gates to `node_exists_by_parts` (`docs/llds/quine-client.md § node_exists_by_parts`), which embeds `idFrom()` in the query text and lets Quine compute the real address.

## Decisions and Alternatives

| Decision | Chosen | Alternative | Rationale |
|---|---|---|---|
| Token source | `GITHUB_TOKEN` env var | Config file | Tokens are secrets; config files are often committed or shared |
| PR → Fix condition | Merged only | All closed | Closed-unmerged PRs were rejected; including them would add noise Fix nodes with no commit link |
| Closing references | Body parsing + API | API only | GitHub's `closing_issues_references` field isn't available on all API versions; body parsing is a reliable fallback |
| Dependabot detection | `user.login == "dependabot[bot]"` | Label-based | Login is stable; labels vary per repo |
| HTTP client | `httpx` (async) | `requests` (sync) | Consistent with the rest of modok's async pattern |
| `ingest_issue`/`ingest_pr` internals | Normalize to `IngestEvent`, dispatch to `run_ingest_event` | Keep inline `upsert_node`/edge-writing logic, duplicated from the webhook path | Discovered live that the poll/batch path (this module) and the webhook path (`GitHubAdapter` + `run_ingest_event`) had drifted into two independently-maintained implementations, one of them (webhook's PR handling) meaningfully less complete than the other — see `docs/llds/continuous-ci-ingestion.md § Prerequisite` for the full finding and the parity-test discipline gating this change |

## Open Questions

1. Should `Fix -[:IMPLEMENTED_IN]-> Commit` be traversed by the DRE to surface the specific fix commit in the debug packet? Deferred — `pr_url` already links to the PR (which shows the merge commit), and adding a Fix→Commit traversal expands the DRE's scope. Revisit when there is a concrete use case for surfacing fix commit SHAs directly.
