# Customize MODOK for Your Project

Assumes [`docs/project-setup.md`](project-setup.md) is done: your project is registered, registries are bootstrapped, and first ingestion has run. This doc covers the knobs you actually control per project — the things worth deliberately setting up, not just leaving as whatever `modok init`/`import-arrow` produced.

This is **not** a guide to writing a new ingestion adapter for another ticketing system (Jira, Linear, Zendesk, etc.). Ingestion already has an adapter shape (`PullAdapter` protocol, `source_system` as a generic string on every `CustomerIssue`) — see `docs/llds/webhook-receiver.md § Pull adapter` if you want to build one. GitHub write-back, on the other hand, is currently GitHub-specific with no equivalent abstraction. Both are code-level work, out of scope here.

---

## Registry curation quality — this is your biggest lever

`registries/features.yml`'s `source_files:` list for a feature is not just documentation — it directly determines which files get the DRE's strongest evidence tier (`feature_primary_file`, score 9.0) versus its weakest (`feature_anchor`, score 3.0, for files only reachable via one of the feature's modules but not in this list).

Found live: a feature whose `source_files:` list was auto-populated as the union of *all* its modules' files — including modules only tangentially related to the feature (an OS-image build script, a general health monitor) — gave those tangential files the same top-tier evidence as the feature's actual implementation files. No amount of scoring-engine tuning fixes this; it's a data problem. If your debug packets seem to be surfacing the wrong files for a feature, check this list first:

```yaml
wifi-provisioning:
  modules:
  - stagehand-ble
  - stagehand-wifi-provision
  - chroot-customize        # touches wifi during OS image build — tangential
  source_files:
  - client/wifi_provision_logic.py   # the actual implementation
  - pi-image/chroot-customize.sh      # ended up here too — too broad
```

Keep `source_files:` curated to the files that most directly implement the feature. It's fine for `modules:` to be broader (those files are still reachable and still contribute evidence, just at a lower, non-corroborating tier) — the `source_files:` list is the one field worth being deliberate about.

**Scoring weights themselves are not configurable per project** — they're constants in `src/modok/retrieval/engine.py`. If you want to understand the reasoning behind them (and why registry curation matters as much as it does), see `docs/scoring-brainstorm.md`.

---

## GitHub issue labels — required for `ticket_kind`

`new-bug-report-pattern` (one of the six standing queries — see below) fires on `CustomerIssue.ticket_kind == "bug"` alone. That field is derived from GitHub issue labels via case-insensitive substring match (`ticket_kind_from_labels`): any label containing `"bug"` sets `ticket_kind = "bug"`; `"feature"` or `"enhancement"` sets `ticket_kind = "feature_request"`. No label containing either substring means `ticket_kind` stays unset, and that standing query never fires for the ticket.

Set this up in your GitHub repo:

1. Create (or confirm you already have) labels named or containing `bug` and `feature`/`enhancement`.
2. Add an issue template that requires the reporter to pick one — a ticket filed without a matching label is invisible to `new-bug-report-pattern` regardless of how clearly it describes a bug.

---

## GitHub write-back config

Two settings enable the write-back comments (`docs/llds/standing-queries.md § GitHub Write-Back`):

- `github_repo` — set on the project's entry in `~/.modok/config.toml` (e.g. `github_repo = "yourorg/yourproject"`).
- `GITHUB_TOKEN` — an environment variable, not in `config.toml` (same token `ingest-github` already uses for reading issues/PRs; needs comment-write scope).

Both must be present or write-back is silently skipped (not an error — useful if you want ingestion/standing-queries running without posting anything back yet). When both are set, every standing-query match posts two comments: an immediate acknowledgment (no LLM call), then the full debug packet once `retrieve()` finishes. The same two settings also gate the File & Root-Cause Escalation patterns (below), which create new issues rather than comment on existing ones.

---

## Dependency-graph ingestion config

Dependency-graph ingestion (package/version topology, manifest-change history, `File -[:USES_DEPENDENCY]-> DependencyPackage` mapping — see `docs/llds/dependency-graph-ingestion.md`) reuses the same `github_repo`/`GITHUB_TOKEN` already set up above for issue/PR polling. There's nothing extra to configure for the basic case: a merged PR touching a tracked manifest (`requirements*.txt` or `pyproject.toml`'s `[project.dependencies]` in v1 — other ecosystems are detected so a touching PR isn't silently ignored, but not yet parsed) is picked up automatically on the same 30-second poll cycle, on its own cursor (`last_dependency_sync`, written automatically — nothing to set by hand).

Two optional knobs, both opt-in:

- `dependency_manifest_globs` — a list of glob patterns on the project's `[[projects]]` entry in `~/.modok/config.toml`:

  ```toml
  [[projects]]
  slug = "stagehand"
  repo = "/Users/you/github/stagehand"
  github_repo = "yourorg/stagehand"
  dependency_manifest_globs = ["client/**"]
  ```

  Narrows which manifest paths are tracked — useful in a monorepo to skip vendored or unrelated manifests elsewhere in the tree. Unset (the default) tracks every manifest file the static detection table recognizes, anywhere in a touched PR's diff.

- `.modok/dependency-map.yml` — a small, checked-in, human-maintained file at the repo root, for the cases where an import name doesn't match its package name (e.g. `cv2` → `opencv-python`, `yaml` → `PyYAML`):

  ```yaml
  import_overrides:
    cv2: opencv-python
    yaml: PyYAML
  ```

  Without an entry, MODOK assumes the import name *is* the package name — correct for the large majority of packages (`bleak` imports as `bleak`). This mapping is purely mechanical; MODOK never uses an LLM to guess it.

---

## Webhook push vs. poll — pick one per project

- **Push** (real GitHub webhook, requires a public endpoint or tunnel — ngrok, ngrok-alternative, or a real deployment): near-instant ingestion on issue events.
- **Poll** (`[webhook] github_poll_enabled = true`, `github_poll_interval_seconds`, default 30): `modok serve` fetches new/changed issues on an interval, no public endpoint needed. This is what makes the whole loop demoable from a laptop with no tunnel — see `docs/standing-query-demo.md`.

Both write to the same `CustomerIssue` nodes through the same ingestion path; picking one is purely about how new tickets get *noticed*, not how they're processed afterward.

---

## Standing queries are fixed — you don't author your own

Six standing queries exist, installed by `modok stream install`, and MODOK does not expose standing-query authoring to projects (a deliberate non-goal — see `docs/high-level-design.md § Non-Goals`):

- `actionable-issue-pattern` — fires when a `CustomerIssue`'s error signature connects, through the graph, to a `KnownIssue` that already has a confirmed `Fix`. The strictest of the six; requires prior-knowledge graph structure to already exist.
- `new-bug-report-pattern` — fires on `ticket_kind == "bug"` alone (see labels, above). The most permissive; fires on essentially every bug report regardless of what else is known.
- `error-flagged-pattern` — fires when a `CustomerIssue` has any `HAS_ERROR` edge at all, whether or not it resolves to a known fix.
- `ci-corroboration-pattern` — fires when a `CustomerIssue`'s error signature is independently corroborated by a CI test failure carrying the same canonical error (`docs/llds/continuous-ci-ingestion.md`).
- `file-escalation-pattern` — fires when three or more open tickets independently flag the same file as a high-confidence debug candidate since that file's last commit. See § File & Root-Cause Escalation, below — this one creates a new GitHub issue, not just a comment.
- `root-cause-escalation-pattern` — fires when three or more currently-open tickets independently affect the same feature. Also below.

There's nothing to configure here beyond making sure your tickets carry the labels/error signatures the first three key off of — the escalation patterns key off graph structure (`AFFECTS`/high-confidence candidates) that's already populated by existing ingestion, with nothing extra to set up.

---

## File & Root-Cause Escalation

Unlike the other four standing queries (which comment on an *existing* ticket), these two **create new GitHub issues** — worth knowing about before they surprise you in your issue tracker.

- **File Escalation** (`file-escalation-pattern`) — when 3+ open tickets flag the same file as a high-confidence debug candidate since that file's last commit, MODOK opens a new issue (label `modok-escalation`) linking the contributing tickets, and keeps adding to it (as a comment) as further tickets flag the same file. A new commit to the file resets the window — the next 3 qualifying tickets after that commit open a fresh issue rather than piling onto the old one. See `docs/llds/file-escalation-pattern.md`.
- **Root-Cause Escalation** (`root-cause-escalation-pattern`) — when 3+ *currently-open* tickets affect the same feature, MODOK opens a new issue (label `modok-root-cause`, colored orange) grouping them. Unlike File Escalation, there's no code-change reset — **you close the issue yourself** once the batch is handled; the next 3 open, not-yet-grouped tickets on that feature then open a fresh one. Tickets already counted toward an escalation (open or closed) are never recounted toward a later one. Closing the issue on GitHub is reflected in the graph's `RootCauseEscalation.status` field within one poll cycle (a separate sync sweep from the escalation logic itself, added after this was found missing during live testing — the field previously never updated), so a query like `MATCH (n) WHERE n.node_type = 'RootCauseEscalation' AND n.status = 'open' RETURN n` reflects current reality, not just what MODOK last wrote at creation time. See `docs/llds/root-cause-escalation-pattern.md`.

Both reuse the same `github_repo`/`GITHUB_TOKEN` config as the rest of GitHub write-back (above) — nothing extra to set up, and both are silently skipped (not an error) if either is missing.

A ticket **deleted** (not closed) on GitHub is detected within one poll cycle and marked `status = "deleted"` in the graph — it stops counting toward either escalation's threshold automatically, the same way a closed ticket does. This is a correctness fix, not something to configure (`docs/llds/github-ingestion.md § Deleted Ticket Detection`).

**Tickets investigated before these patterns existed won't count until backfilled.** `FLAGS` is only ever computed at a ticket's *first* investigation-triggering standing-query match — `DistinctId` fires at most once per ticket, ever, so an already-investigated ticket never gets a second chance to have `retrieve()`/`FLAGS` run through the normal write-back path. Run `modok backfill-flags --project <slug>` once to catch these up — it finds every open GitHub ticket with no `FLAGS` edge yet, computes it, and backfills `created_at` too if it's missing (using the ticket's earliest `Investigation.triggered_at` as the best available proxy for tickets that predate that field). Safe to re-run; it skips anything already flagged.

---

## Re-run `ingest-elements` as code changes

`registries/elements.yml` (class names, method names, signal names extracted via AST/regex) feeds element-anchor matching — the mechanism that lets a ticket saying "reinit button" match a module containing `reinit_requested` even without an exact name match. It goes stale as source files are added, removed, or renamed. Re-run `modok ingest-elements --project <slug>` after any substantial refactor; there's no automatic trigger for it today.
