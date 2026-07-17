# MODOK CLI Reference

Every `modok` command, its flags, and what it does. Generated against the actual registered commands (`src/modok/cli/main.py`) and their live `--help` output, not reconstructed from memory — if this drifts from `modok <command> --help`, the CLI is the source of truth and this doc is stale.

For a guided walkthrough instead of a flag-by-flag reference, see [`docs/setup.md`](setup.md) (platform install), [`docs/project-setup.md`](project-setup.md) (add a project, first ingestion), and [`docs/customize-for-your-project.md`](customize-for-your-project.md) (the knobs worth deliberately setting).

---

## Global options

```
modok [OPTIONS] COMMAND [ARGS]...
```

| Flag | Description |
|---|---|
| `--version` | Show the installed MODOK version and exit. |
| `--status` | Show Quine connectivity and a graph summary (node/edge counts), then exit. Does not require a `--project` — this is instance-level, not project-level. |
| `--help` | Show the command list and exit. |

Every subcommand also accepts `--help` for its own options.

---

## Project setup

Commands that bootstrap a project — registries, code map, arrow-doc import. See `docs/project-setup.md` for the order these are meant to run in.

### `modok init`

```
modok init --project TEXT --repo PATH [--assisted]
```

Registers a project with MODOK: creates stub registry files, installs a git post-commit hook, updates `~/.modok/config.toml` with a `[[projects]]` entry.

| Flag | Required | Description |
|---|---|---|
| `--project` | yes | Project slug. |
| `--repo` | yes | Path to the project's git repo. |
| `--assisted` | no | Use the LLM to propose registry contents from existing docs (`docs/high-level-design.md § Key Design Decision #8`), instead of leaving stub files empty. Split across two passes — run `modok normalise` afterward to finish. |

### `modok extract-code-map`

```
modok extract-code-map --project TEXT [--repo TEXT] [--output TEXT]
```

Scans the repo and writes `.modok/code-map.yml`: every file's path, SHA-256 hash, role (source/test/config/docs/generated/ignored), line count, and — for Python — extracted symbols and imports via `ast`. Deterministic; the same repo state always produces the same code map.

| Flag | Required | Description |
|---|---|---|
| `--project` | yes | Project slug. |
| `--repo` | no | Repo root path, overriding what's in config. |
| `--output` | no | Output path (default: `<repo>/.modok/code-map.yml`). |

`modok ingest` runs this automatically if no code map exists yet — you don't need to run it by hand before a first ingestion. Re-run it manually any time the repo's file structure changes significantly.

### `modok import-arrow`

```
modok import-arrow --project TEXT [--repo TEXT] [--dry-run] [--no-llm]
```

Extracts `registries/features.yml` and `registries/modules.yml` directly from a project's `docs/arrows/index.yaml` and each arrow doc's `### Code`/`### Key Components` sections, validating every file path against the code map. Preferred over LLM-from-docs extraction (`init --assisted`) when arrow docs exist — more accurate, fewer LLM calls.

| Flag | Required | Description |
|---|---|---|
| `--project` | yes | Project slug. |
| `--repo` | no | Repo root path, overriding config. |
| `--dry-run` | no | Print the proposed registry output; write nothing. |
| `--no-llm` | no | Skip both LLM passes (name/description generation, duplicate-module resolution) — useful for CI or a first pass. |

### `modok normalise`

```
modok normalise --project TEXT
```

The second half of `modok init --assisted`'s registry bootstrap: reads `features.raw.yml`/`modules.raw.yml`/`errors.raw.yml`, normalises each field type via its own LLM call, runs a CEGIS verification loop to catch invented concepts, and writes the final `features.yml`/`modules.yml`/`errors.yml`. Split into its own command so an hour-long enrichment run is never lost to a normalisation timeout.

| Flag | Required | Description |
|---|---|---|
| `--project` | yes | Project slug. |

---

## Ingestion

Commands that write to the Quine graph. Run in this order for a first ingestion (`docs/project-setup.md § Step 5`): `ingest` → `ingest-git` → `ingest-elements`. `ingest-github` is independent (issues/PRs, not docs/git).

### `modok ingest`

```
modok ingest --project TEXT [TICKET_FILE]
```

Ingests docs and registries into the graph (Feature, Module, File, DocSection nodes and their edges) — mechanical, three-tier doc discovery (arrow-index-driven, path-based inference, `unregistered` fallback). Requires a code map; generates one automatically if absent.

Pass a `TICKET_FILE` path instead to ingest a single ticket file directly (bypasses the doc-discovery path entirely — used by the git post-commit hook `modok init` installs, and for manual one-off ticket ingestion).

| Argument/Flag | Required | Description |
|---|---|---|
| `--project` | yes | Project slug. |
| `TICKET_FILE` | no (positional) | Path to a single ticket file to ingest, instead of running full doc ingestion. |

### `modok ingest-git`

```
modok ingest-git --project TEXT [--repo TEXT] [--full] [--since DATE] [--max-commits INTEGER]
```

Imports git commit history for registered files as `Commit` nodes with `TOUCHES` edges to `File` nodes. Incremental by default — subsequent runs only import commits since the last run.

| Flag | Required | Description |
|---|---|---|
| `--project` | yes | Project slug. |
| `--repo` | no | Repo path, overriding config. |
| `--full` | no | Import full history, no lookback limit. Mutually exclusive with `--since`. |
| `--since` | no | Import commits authored after this ISO-8601 date. Mutually exclusive with `--full`. |
| `--max-commits` | no | Cap on commits imported (default `500`). |

### `modok ingest-github`

```
modok ingest-github --project TEXT [--full]
```

Pulls GitHub issues (`CustomerIssue` nodes) and merged PRs (`Fix` nodes, plus `IMPLEMENTED_IN`/`RESOLVED_BY` edges where resolvable) via the GitHub REST API. Requires `github_repo` in the project's config entry and `GITHUB_TOKEN` in the environment. Incremental by default (`last_github_sync`); this is the batch/manual equivalent of what `modok serve`'s poll adapter does automatically every 30s when `github_poll_enabled = true`.

| Flag | Required | Description |
|---|---|---|
| `--project` | yes | Project slug. |
| `--full` | no | Fetch all issues/PRs regardless of `last_github_sync`. |

### `modok ingest-elements`

```
modok ingest-elements --project TEXT
```

Extracts code identifiers (class names, method names, signal names — AST for Python, regex for C/C++) from each module's registered source files and writes `registries/elements.yml`. Does not touch the graph or require Quine running; feeds `modok retrieve`'s element-anchor matching (letting a ticket saying "reinit button" match a module containing `reinit_requested`) and mechanical feature anchor linking. Re-run after adding, removing, or substantially renaming source files — nothing triggers this automatically.

| Flag | Required | Description |
|---|---|---|
| `--project` | yes | Project slug. |

---

## Query & retrieval

Read-only commands — none of these write to the graph.

### `modok retrieve`

```
modok retrieve --project TEXT (--ticket TEXT | --node-id INTEGER) [--stream]
```

Assembles and prints a full debug packet (JSON) for a `CustomerIssue`, by ticket ID or, for power users, a raw Quine node ID. `--ticket` and `--node-id` are mutually exclusive; exactly one is required.

| Flag | Required | Description |
|---|---|---|
| `--project` | yes | Project slug. |
| `--ticket` | one of these two | Ticket ID to look up by `(project_slug, ticket_id)`. |
| `--node-id` | one of these two | Raw Quine node ID (power-user escape hatch). |
| `--stream` | no | Emit NDJSON progress lines (`{"step": ..., "data": ...}`) as retrieval proceeds, before the final `{"step": "complete", ...}` line — useful for a live UI or for watching a slow retrieval (the LLM summary step can take a while) rather than waiting silently. |

### `modok recall`

```
modok recall --project TEXT (--feature TEXT | --module TEXT) [--json]
```

Returns everything MODOK knows about one already-registered feature or module slug: parent/child relationships, source files, test files. Reads the registries directly — works even before `modok quine start`.

| Flag | Required | Description |
|---|---|---|
| `--project` | yes | Project slug. |
| `--feature` | one of these two | Feature slug. |
| `--module` | one of these two | Module slug. |
| `--json` | no | Output as JSON instead of formatted text. |

### `modok search`

```
modok search --project TEXT [QUERY] [--section TEXT] [--text TEXT] [--json]
```

Keyword search across the graph. The positional `QUERY` argument is shorthand for `--text`.

| Argument/Flag | Required | Description |
|---|---|---|
| `--project` | yes | Project slug. |
| `QUERY` | no (positional) | Shorthand for `--text`. |
| `--section` | no | Substring match against doc section headings. |
| `--text` | no | Substring match across all node properties. |
| `--json` | no | Output as JSON. |

### `modok list`

```
modok list --project TEXT [--features] [--modules] [--elements] [--json]
```

Lists every registered feature/module slug (and module elements), alphabetically with names. The command to run first if you don't already know a valid slug for `recall`/`diagnose`.

| Flag | Required | Description |
|---|---|---|
| `--project` | yes | Project slug. |
| `--features` | no | List features only. |
| `--modules` | no | List modules only. |
| `--elements` | no | List module elements only. |
| `--json` | no | Output as JSON. |

### `modok diagnose`

```
modok diagnose --project TEXT --feature TEXT [--error TEXT] [--symptom TEXT] [--json]
```

Assembles a debug packet anchored directly on a feature slug (rather than a `CustomerIssue`) — for exploring "what does MODOK know about this feature" without a real ticket.

| Flag | Required | Description |
|---|---|---|
| `--project` | yes | Project slug. |
| `--feature` | yes | Feature slug to anchor the traversal on. |
| `--error` | no | Error signature slug (`normalized_error`) to narrow results. |
| `--symptom` | no | Substring match against `KnownIssue` summaries. |
| `--json` | no | Output as JSON. |

---

## Live operation

Commands for running MODOK continuously — the local Quine process, standing queries, and the webhook/poll server.

### `modok quine`

```
modok quine COMMAND
```

Manages the local Quine JAR process as a convenience wrapper — not required if you manage Quine yourself (e.g. the shared Mac mini's `launchd` service, `docs/setup.md § Persistent Quine service`).

| Subcommand | Description |
|---|---|
| `modok quine start` | Start Quine as a background process. |
| `modok quine stop` | Stop the running Quine process. |
| `modok quine status` | Report whether Quine is running and reachable. |

None of the three take project-specific flags — Quine is one shared instance for every project.

### `modok stream`

```
modok stream COMMAND
```

Manages the fixed set of Quine standing queries (`docs/llds/standing-queries.md`) — instance-level infrastructure, like `quine`, not per-project (project isolation comes from `idFrom()` node-address topology, not from separate standing queries per project). No `--project` flag on any subcommand.

| Subcommand | Description |
|---|---|
| `modok stream install` | Install every standing query definition in `src/modok/quine/standing_queries/` that isn't already installed. Idempotent — reports `already installed` for ones that are. Currently six: `actionable-issue-pattern`, `new-bug-report-pattern`, `error-flagged-pattern`, `ci-corroboration-pattern`, `file-escalation-pattern`, `root-cause-escalation-pattern`. |
| `modok stream status` | List installed standing query names. |
| `modok stream remove` | Remove every standing query definition that's currently installed — loops over the same checked-in definition set `install` does, reporting `removed` or `not installed` per pattern. Removes all six, not a single one; there's no flag to target just one. |

### `modok serve`

```
modok serve [--host TEXT] [--port INTEGER]
```

Starts the webhook receiver: the push-adapter HTTP server (`POST /webhook/{project}/{source}`), the standing-query result route (`POST /standing-query/result`), and — for any project with `github_poll_enabled = true` — the 30-second GitHub poll loop (issues/PRs, CI activity, dependency changes, both escalation patterns' reconciliation sweeps, deleted-ticket detection). Leave running in its own terminal/process for continuous operation.

| Flag | Required | Description |
|---|---|---|
| `--host` | no | Bind address (default `127.0.0.1`). |
| `--port` | no | Bind port (default `4242`). |

### `modok backfill-flags`

```
modok backfill-flags --project TEXT
```

One-time catch-up: computes `FLAGS` edges (and backfills `created_at` where missing) for open GitHub tickets whose one-time investigation fired before the File/Root-Cause Escalation patterns existed — `DistinctId` standing queries fire at most once per ticket ever, so an already-investigated ticket never gets a second chance to have this computed through the normal write-back path (`docs/llds/file-escalation-pattern.md § FLAGS Write-Back`). Safe to re-run — skips any ticket that already has a `FLAGS` edge. See `docs/customize-for-your-project.md § Tickets investigated before these patterns existed`.

| Flag | Required | Description |
|---|---|---|
| `--project` | yes | Project slug. |

---

## Environment variables

Not flags, but every command that touches GitHub reads these from the environment rather than `config.toml` (secrets don't belong in a config file):

| Variable | Used by |
|---|---|
| `GITHUB_TOKEN` | `ingest-github`, `modok serve`'s poll adapter, all GitHub write-back (comments, escalation issue creation, label coloring, deleted-ticket detection). Needs comment-write and issue-create scope for the write-back paths, read-only scope is sufficient for `ingest-github` alone. |
