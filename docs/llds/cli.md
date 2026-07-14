# CLI

## Context and Design Philosophy

The MODOK CLI is the primary development interface for ingesting docs, querying the graph, initializing projects, and managing Quine's lifecycle. It is a thin entry point: argument parsing, config loading, client construction, and output formatting live here; all logic lives in the four core components (`modok.ingestion`, `modok.retrieval`, `modok.quine`, `modok.llm`).

The CLI is designed so an agent can call it directly via subprocess — it exits with a non-zero code on failure, writes structured output to stdout, and writes human-readable diagnostics to stderr. This makes it usable both interactively and as a tool for automated pipelines without a dedicated MCP server.

Guiding principles:
- **Thin.** No business logic here. Each command is ≤ 30 lines: parse args → load config → call core → format output.
- **`--project` everywhere.** Every command that touches the graph requires `--project <slug>`. No ambient project state.
- **Quine-first startup.** Every graph-touching command pings Quine on startup and exits with a clear actionable error if it's unreachable.
- **Structured stdout, human stderr.** Stdout carries data (JSON or tabular) the caller can parse. Stderr carries progress, warnings, and prompts.
- **Exit codes.** `0` = success, `1` = user error (bad args, missing config, issue not found), `2` = infrastructure error (Quine down, LLM unreachable), `3` = partial success (ingestion completed with errors).

## Command Surface

```
modok --version
modok --help
modok --status

modok init        --project <slug> --repo <path> [--assisted]
modok ingest      --project <slug>
modok ingest-git      --project <slug> [--full] [--since <date>] [--max-commits <n>]
modok ingest-github   --project <slug> [--full]
modok ingest-elements --project <slug>
modok retrieve    --project <slug> --ticket <id>
               [--node-id <int>]
modok recall   --project <slug> (--feature <slug> | --module <slug>) [--json]
modok search   --project <slug> (QUERY | --section <str> | --text <str>) [--json]
modok list     --project <slug> [--features] [--modules] [--elements] [--json]
modok diagnose --project <slug> --feature <slug>
               [--error <slug>] [--symptom <str>] [--json]
modok quine    (start | stop | status)
modok stream   (install | status | remove)
```

### `modok init`

Initializes a project in MODOK:
1. Verifies `--repo <path>` contains a `.git/` directory; exits `1` with "not a git repository: `<path>`" if absent. A repo without `.git/` is almost certainly a wrong path — the hook is a core part of init, and silently skipping it would leave the project half-initialized.
2. Without `--assisted`: creates stub `features.yml`, `modules.yml`, `errors.yml` in `<repo>/registries/` if missing.
   With `--assisted`: runs the Registry Proposal Engine (delegates to `modok.registry.proposal`) to discover docs, extract typed nodes, and write proposed registry files. Overwrites stubs if present. Exits `2` if the LLM gateway is unreachable.
3. Installs a post-commit git hook in the project repo (delegates to `modok.ingestion.hook`).
4. Registers the project in `~/.modok/config.toml` under `[[projects]]` if not already present.

Does **not** run ingestion. Does **not** require Quine to be running (except `--assisted` requires the LLM gateway).

`--assisted` prints progress to stderr as it processes each doc section, and prints a summary to stdout on completion:
```
Processed 15 sections across 3 docs.
Wrote registries/features.yml  (8 features)
Wrote registries/modules.yml   (5 modules)
Wrote registries/errors.yml    (16 error signatures)
```

Exit codes for `modok init`: `0` = success, `1` = bad args or not a git repo, `2` = LLM gateway unreachable (only with `--assisted`).

### `modok ingest`

Runs the doc ingestion pipeline for `--project <slug>`:
1. Pings Quine; exits `2` if unreachable.
2. Loads registries from `{repo_root}/registries/`.
3. Calls `discover_docs(repo_root, registry)` to classify all `docs/**/*.md` via three-tier discovery: Tier 1 (arrow index), Tier 2 (path + stem inference), Tier 3 (unregistered).
4. Calls `run_ingestion(repo_root, registry, client, project_slug)`, which writes the following node types to Quine:
   - **Feature, Module, File** — derived automatically from the registry.
   - **DocSection** — extracted from heading structure of every registered doc.
   - **Doc** — written for unregistered docs only (no feature association).
   - **KnownIssue** — only written when a doc contains a `known_issue` MODOK block. These must be authored manually in the relevant doc.
   - **ErrorSignature** — only written when a doc contains an `error_signatures` entry. These must be authored manually.
   - **Fix** — only written when a doc contains a `fix` MODOK block. These must be authored manually.
5. Prints the structured ingestion report to stdout.
6. Exits `3` if the report contains errors; `0` otherwise.

No `<path>` argument — repo root is derived from the project's `repo` in `~/.modok/config.toml`. No `--fix` flag — frontmatter is not the source of truth; metadata comes from the registry.

### `modok ingest-git`

Ingests git commits touching registered files into Quine as `Commit` nodes with `TOUCHES` edges to `File` nodes. **Requires `modok ingest` to have run first** — `TOUCHES` edges are only written to `File` nodes already in the graph.

1. Pings Quine; exits `2` if unreachable.
2. Loads registries and builds the registered file set from `features.yml` and the arrow index.
3. Determines incremental start point from `last_git_sha` in `~/.modok/config.toml` for this project.
4. Runs `git log` filtered to registered files only (commits touching only unregistered files are skipped).
5. Writes each commit as a `Commit` node; writes `TOUCHES` edges only to `File` nodes already in the graph.
6. On success, updates `last_git_sha` to HEAD in `~/.modok/config.toml`.

**Incremental behavior:**
- With `last_git_sha` set: ingests only commits after that SHA (`{sha}..HEAD`).
- With no `last_git_sha` and no flags: ingests the last 6 months of history (up to `--max-commits`, default 500).
- `--full`: ingests the entire repo history. Mutually exclusive with `--since`.
- `--since <date>`: ingests commits after the given date (any format `git log --after` accepts, e.g. `2025-01-01`). Mutually exclusive with `--full`.
- `--max-commits <n>`: caps the number of commits processed (default 500). Ignored when `--full` is set.

Exits `0` on success (including when there are no new commits). Exits `2` if Quine is unreachable.

### `modok ingest-github`

Pulls GitHub issues and merged PRs for the project and writes them to Quine.

1. Pings Quine; exits `2` if unreachable.
2. Reads `github_repo` from project config; reads `GITHUB_TOKEN` from environment. Exits `1` if either is missing.
3. Fetches issues (all states) and merged PRs from the GitHub API, incrementally since `last_github_sync`.
4. Writes `CustomerIssue` nodes (one per issue) and `Fix` nodes (one per merged PR).
5. Writes `Fix -[:IMPLEMENTED_IN]-> Commit` edges using PR `merge_commit_sha` (silently skipped if Commit absent).
6. Parses PR closing references and writes `CustomerIssue -[:RESOLVED_BY]-> Fix` edges.
7. Updates `last_github_sync` in config on success.

**Incremental behavior:**
- With `last_github_sync` set: fetches only issues/PRs with `updated_at` after that timestamp.
- With no `last_github_sync` or `--full`: fetches all issues and PRs.

Exit codes: `0` on success, `1` if config or token is missing, `2` if Quine or GitHub API is unreachable.

See `docs/llds/github-ingestion.md` for full design.

### `modok ingest-elements`

Extracts code identifiers from each module's source files and writes them to `registries/elements.yml`. Run this after `modok ingest` and `modok ingest-git` — it enriches the registry used at query time by `modok retrieve` but does not depend on those commands having run. Re-run any time module source files are added, removed, or substantially renamed.

1. Loads registries from `{repo_root}/registries/`.
2. For each module in `modules.yml` that has a `source_files` list, calls `extract_module_elements(source_files, repo_root)` to extract identifiers:
   - **Python files**: AST-based extraction of class names, non-dunder method names, and class-level attribute names (covers Qt signals and similar patterns).
   - **C/C++ files**: regex-based extraction of function-call-like identifiers, filtered against common C keywords.
   - Capped at 25 identifiers per module to keep prompts concise.
3. Writes `registries/elements.yml` with shape `{module_slug: [identifier, ...]}`.
4. If any module entries in `modules.yml` contain a stale `elements` key (written by an older version), strips those keys and rewrites `modules.yml`.

Does **not** require Quine to be running. Does **not** write to the graph — `elements.yml` is read at startup and forwarded to the LLM gateway when `modok retrieve` runs.

Exit codes: `0` on success, `1` if the project is not in config or the registries directory is missing.

### `modok retrieve`

Fetches a debug packet for a customer issue and prints it as JSON to stdout.

**Primary form** (for agents and humans):
```
modok retrieve --project <slug> --ticket <id>
```
Looks up the `CustomerIssue` node via `MATCH (n) WHERE n.project_slug = $p AND n.ticket_id = $t RETURN id(n)`, then calls `retrieve(node_id, project_slug, client)`. Exits `1` if no matching node is found.

**Power-user form** (when node ID is already known):
```
modok retrieve --project <slug> --node-id <int>
```
Skips the graph lookup and calls `retrieve` directly with the supplied integer.

`--ticket` and `--node-id` are mutually exclusive. Supplying both or neither exits `1`.

> **Note:** `--source` was intentionally omitted. The current lookup assumes ticket IDs are unique within a project. If multi-source disambiguation becomes necessary (e.g. Zendesk and Jira sharing ticket IDs in the same project), add `--source` back and switch the lookup to `idFrom("customer-issue", project_slug, source_system, ticket_id)`. The `source_system` field is already stored on `CustomerIssue` nodes via ingest.

Exit codes: `0` on success, `1` if the issue is not found in the specified project or args are invalid, `2` if Quine or the LLM gateway is unreachable. `DRENotFoundError` maps to exit `1` with the message "issue not found in project `<slug>`".

### `modok recall`

Returns everything MODOK knows about a feature or module slug. Read-only graph traversal; not tied to a customer issue.

Accepts `--feature <slug>` or `--module <slug>` (or both). At least one is required.

- `--feature`: traverses from `Feature {project_slug, feature_slug}` along all outbound edges and returns the feature node plus all directly connected nodes.
- `--module`: traverses from `Module {project_slug, module_slug}`, resolves its implementing feature (if any) and its source files.

Results are deduplicated when both flags are supplied. Prints a human-readable tabular summary to stdout by default; `--json` emits JSON.

Exit codes: `0` on success (including empty results), `1` if args are malformed or the project is not in config, `2` if Quine is unreachable.

### `modok search`

Substring search across graph node properties. Does not require a known slug — use this when you have a keyword but not the exact feature or module slug.

Accepts one of:
- **Bare `QUERY` argument**: shorthand for `--text <QUERY>`.
- **`--section <str>`**: searches only `DocSection` nodes, matching against `heading_text`. Results are ordered by `doc_path`, `line_start`.
- **`--text <str>`**: searches all nodes, matching against `heading_text`, `name`, `summary`, `normalized_error`, `module_slug`, `feature_slug`, and `repo_path`.

`QUERY` and `--text` are mutually exclusive. At least one search mode is required. Both `--section` and `--text` (or bare QUERY) may be supplied together; results are deduplicated by node ID.

All string comparisons are case-insensitive (`toLower() CONTAINS`).

Prints tabular output by default; `--json` emits `{"project": "<slug>", "nodes": [...]}`.

Exit codes: `0` on success (including empty results), `1` if args are invalid or the project is not in config, `2` if Quine is unreachable.

### `modok list`

Lists the valid feature and module slugs for a project — the discovery command for `recall`'s `--feature`/`--module` and `diagnose`'s `--feature`, both of which require an exact, already-known slug and silently return "(no results)" on a near-miss (e.g. `--feature client` when the real slug is `client-ui`).

Reads directly from the project's registries (`registries/features.yml`, `registries/modules.yml`, `registries/elements.yml`) via the existing `Registry` class (`feature_slugs()`/`module_slugs()`/`feature_names()`/`module_names()`/`module_elements()`) — no Quine query. Does not require Quine to be running.

Three independent flags: `--features`, `--modules`, `--elements`. With none supplied, all three sections are included. Any subset of flags narrows to just those sections. All flags together behave identically to none — there is no "conflicting flags" error state.

Entries within the features/modules sections are sorted alphabetically by slug — registry-file (YAML) order is insertion order, not a meaningful sequence to preserve, and alphabetical is easier to scan for the "what's the exact slug" lookup this command exists for. The elements section is sorted alphabetically by *module* slug, matching the other two sections; element names within a module keep the order `ingest-elements` extracted them in (source-file scan order), since that order isn't arbitrary in the same way YAML mapping order is.

Prints tabular output by default — one `<slug>  <name>` line per entry for features/modules, under a `Features:` / `Modules:` header; one `<module-slug>  <elem1, elem2, ...>` line per module for elements, under an `Elements:` header. Only modules with at least one registered element appear (`ingest-elements` itself never writes an empty entry — see `docs/llds/ingestion-pipeline.md` — so this isn't a further filter, just a consequence of what's in the registry). A section is omitted from tabular output entirely when it wasn't requested; a *requested* section with zero entries (registry file empty, or `ingest-elements` never run) still prints its header with `(none)` below it, so an empty registry is visibly distinct from "you only asked for the other lists."

`--json` emits `{"project": "<slug>", "features": [{"slug": ..., "name": ...}, ...], "modules": [...], "elements": [{"module": ..., "elements": [...]}, ...]}`. A key is present whenever that section was requested (including as an empty list `[]` if the registry has no entries); a key is omitted entirely only when its section was never requested at all. This mirrors the tabular distinction: JSON consumers can tell "empty" from "not asked for" the same way a human reading stdout can.

Exit codes: `0` on success (including a project with zero features, modules, or elements), `1` if the project is not in config or its registries cannot be loaded (`RegistryNotFoundError` — e.g. `modok init`/`modok import-arrow` was never run for this project). `elements.yml` is optional at the `Registry` level (`ingest-elements` may never have been run) — its absence does not raise `RegistryNotFoundError`, it just means `module_elements()` returns `{}`.

**Caveat: a listed slug is not guaranteed to be ingested.** `list` reads what's *registered* (`registries/*.yml`), not what's actually been written to Quine. A feature or module can be registered by `import-arrow` before `modok ingest`/`ingest-git` has run (or after a partial ingestion failure) — in that window, `list` will show the slug but `recall --feature <slug>` can still legitimately print "(no results)". This is accepted: checking ingestion status would require `list` to query Quine per slug, reintroducing the Quine dependency this command deliberately avoids (see Decisions & Alternatives).

### `modok diagnose`

Feature-anchored debug packet assembly. Use when you know the feature slug and optionally have an error or symptom to narrow the results. Does not require a `CustomerIssue` node — intended for interactive/manual debugging.

`--feature <slug>` is required. `--error` and `--symptom` are optional filters.

Traversal, in order:

1. **Files** — `Feature -[:IMPLEMENTED_BY]-> Module -[:DEFINED_IN]-> File`. All files for all modules implementing the feature.
2. **KnownIssues** — `Feature -[:HAS_KNOWN_ISSUE]-> KnownIssue`. If `--symptom <str>` is given, only `KnownIssue` nodes whose `summary` contains the substring (case-insensitive) are included.
3. **ErrorSignature → KnownIssues** — `ErrorSignature -[:AFFECTS]-> Feature` where `normalized_error = --error`. If `--error` is given, fetches the matching `ErrorSignature` and traverses back to `KnownIssue` nodes via `HAS_ERROR`. Each `KnownIssue` reached this way that passes the `--symptom` filter (if any) has its `match_count` incremented.
4. **Fixes** — for each `KnownIssue` found: `KnownIssue -[:RESOLVED_BY]-> Fix`.

Results are deduplicated by node ID. `match_count` accumulates across traversals (a `KnownIssue` reachable via both the feature edge and the error signature gets `match_count = 2`). Each result list is sorted descending by `match_count`.

Output shape reuses `DebugPacket` from the DRE so agents get the same structure regardless of entry point. `anchors.feature_slugs` is set from `--feature`; `anchors.error_signatures` from `--error` if given; `anchors.symptoms` from `--symptom` if given. `issue_summary` is set to `"diagnose: <feature_slug>"`. `confidence` is omitted (set to `1.0`) — no anchor sufficiency concept applies here.

Prints tabular output by default; `--json` emits the `DebugPacket` as JSON.

Exit codes: `0` on success (including empty results), `1` if args are invalid or the project is not in config, `2` if Quine is unreachable.

### `modok quine start | stop | status`

Lifecycle convenience wrapper around the Quine JAR process. Does not require `--project`.

- **`start`**: pings Quine first; if it responds, prints "Quine is already running at `<url>`" to stderr and exits `0` — no PID file check, no process launch. If not responding, validates the JAR path from config immediately and exits `1` with "Quine JAR not found at `<path>`" if absent. Then launches `java -Dconfig.file=~/.modok/quine.conf -jar ~/.modok/quine.jar` as a background process, writes PID to `~/.modok/quine.pid`, and polls `ping()` until ready (up to 30s). Exits `2` if startup times out.
- **`stop`**: reads `~/.modok/quine.pid`, sends SIGTERM, waits up to 10s for the process to exit, removes the PID file. Exits `1` if no PID file exists.
- **`status`**: calls `ping()`; prints `running` or `stopped` to stdout. Always exits `0`.

### `modok --status`

Top-level status flag. Does not require `--project`. Pings Quine and reports:

1. **Quine reachability** — `running at <url>` or `not reachable at <url>`.
2. **Node count** — total node count from `MATCH (n) RETURN count(n)`, plus a per-type breakdown from `MATCH (n) RETURN DISTINCT n.node_type, count(n)`. Nodes with `null` `node_type` are shown as `(untyped)`. Only emitted when Quine is reachable.
3. **Projects** — list of configured projects (slug + repo path) from `~/.modok/config.toml`. Emitted regardless of Quine reachability.

Quine version is not reported — Quine does not expose a version endpoint.

Exits `0` whether or not Quine is reachable — "not reachable" is a valid status, not an error.

Example output:
```
Quine:    running at http://127.0.0.1:8080
Nodes:    66 total
  DocSection    48
  File          10
  Module         3
  Feature        2
  (untyped)      3

Projects:
  stagehand     ~/github/stagehand
```

## Config Loading

Config is read from `~/.modok/config.toml` on every command invocation. No global state is mutated. The config schema:

```toml
[quine]
url = "http://127.0.0.1:8080"
jar = "~/.modok/quine.jar"      # used by modok quine start only

[llm]
provider = "ollama"
base_url = "http://127.0.0.1:11434/v1"
model = "llama3"

[[projects]]
slug = "stagehand"
repo = "~/github/stagehand"
last_git_sha = "<sha>"   # written by modok ingest-git; absent on first run
```

`~` in path values is expanded via `Path.expanduser()`. `ConfigNotFoundError` (exit `1`) if `~/.modok/config.toml` is absent; `ConfigParseError` (exit `1`) if malformed. The CLI derives `repo_root` for a project by matching `--project <slug>` against the `[[projects]]` list; an unknown slug exits `1` with a clear error.

Config is modeled as a pydantic model (`ModokConfig`) for validation and type safety. A `ModokConfig.load()` classmethod reads and parses the TOML file.

## Output Format

**Stdout** is the data channel — always parseable by a caller:
- `modok retrieve`: JSON (`DebugPacket.model_dump()`).
- `modok recall`: tabular by default; JSON with `--json`.
- `modok ingest`: structured report (key-value block by default; JSON with `--json`).
- `modok quine status`: single word `running` or `stopped`.
- `modok init`: one confirmation line per action taken.

**Stderr** carries progress, warnings, and error messages. Commands that succeed with no warnings produce no stderr output.

## Framework

`click` is the CLI framework. It handles argument parsing, help text, subcommand grouping, and process exit. Added as a production dependency in `pyproject.toml`. `toml` parsing uses the stdlib `tomllib` (Python 3.11+).

## Module Layout

```
src/modok/cli/
    __init__.py
    main.py            # top-level click group, --version, shared options
    commands/
        __init__.py
        init.py
        ingest.py
        ingest_git.py
        ingest_github.py
        ingest_elements.py
        retrieve.py
        recall.py
        search.py
        list.py
        diagnose.py
        quine.py
        stream.py
        _output.py     # shared graph-result helpers: require_quine(), collect_nodes(),
                        # dedup_nodes(), print_node() — used by recall.py and search.py
    config.py          # ModokConfig pydantic model, load(), path expansion
    output.py          # stdout formatters: json_out(), tabular(), report_out()
    errors.py          # CliError(exit_code, message); caught in main and sys.exit'd
```

Entry point declared in `pyproject.toml`:
```toml
[project.scripts]
modok = "modok.cli.main:cli"
```

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|---|---|---|---|
| CLI framework | `click` | `typer`, `argparse`, `docopt` | `click` is mature, widely understood, and has clean subcommand grouping. `typer` wraps click but adds a layer. `argparse` is verbose. |
| Stdout/stderr split | Data to stdout, diagnostics to stderr | Everything to stdout | Agents calling via subprocess capture stdout without filtering noise. Standard Unix convention. |
| Exit codes | `0/1/2/3` | `0/1` only | Agents and CI need to distinguish "Quine is down" from "bad argument" without parsing stderr. |
| `--project` required everywhere | Always explicit | Ambient project from cwd or config | No ambient state; no cross-project contamination. Agents constructing subprocess calls always know what they're touching. |
| `retrieve` input | `--ticket` (primary); `--node-id` (power user) | `--source` + `--ticket` via `idFrom`; node ID only | `--source` deferred — ticket IDs assumed unique per project for now. `idFrom`-based path retained as the upgrade path when multi-source disambiguation is needed. |
| Config location | `~/.modok/config.toml` fixed | `.modok.toml` in cwd, `$MODOK_CONFIG` env var | Fixed location makes agent subprocess calls predictable without path coordination. |
| Config parsing | stdlib `tomllib` + pydantic | `tomli` backport, `tomlkit` | `tomllib` is in stdlib from Python 3.11 (already required). No extra dependency. |
| `ingest-git` incremental start | `last_git_sha` in config; default 6 months if absent | Always full history; always explicit `--since` | Full history is expensive on large repos. 6-month default covers most active development. `--full` is the explicit escape hatch. | 
| `quine start` when already running | Ping first; if up, print and exit `0` | Check PID file; error if no PID | Quine may have been started externally. "It's already running" is success, not an error, regardless of how it started. |
| `recall` on unknown feature slug | Exit `0` with empty results | Exit `1` | "No results" is a valid graph query answer. Agents can handle empty JSON; they can't easily distinguish a real error from a missing feature if both return non-zero. |
| `init` on non-git directory | Exit `1` immediately | Skip hook, warn, continue | The hook is a core deliverable of `init`. A missing `.git/` is almost certainly a wrong path; a silent skip would leave the project half-initialized with no visible signal. |
| `init --assisted` write behaviour | Write files directly; user edits if needed | Interactive review before write | Simpler. Registry files are source-of-truth text files — editing them is the natural correction mechanism. An interactive approval loop adds ceremony with no safety benefit here. |
| JAR path validation on `quine start` | Check before forking, exit `1` with clear message | Let JVM error surface | JVM errors for missing JARs are unactionable. A path check before fork gives an operator-readable error. |
| `quine stop` when process already dead | Exit `2` with crash message, leave PID file | Treat as success (delete PID, exit `0`) | Crashes should be visible. Silently cleaning up a dead-process PID file hides the fact that Quine crashed between start and stop. The operator needs to check logs. |
| `list` data source | Registries (`Registry` class) directly | Query Quine's `Feature`/`Module` nodes | The registries are already the source of truth for which slugs are valid (HLD: "Convention + registries are truth for structure") — querying Quine would require it to be running just to discover topic names, and could show a slug that was registered but never actually ingested (or vice versa after a partial run). Reading the registry file directly is faster, has no infrastructure dependency, and can never disagree with what `recall`/`diagnose` will accept as a valid slug. |

## Open Questions & Future Decisions

### Deferred
1. **`$MODOK_CONFIG` env var** — config path override for multi-config setups (e.g. staging vs. prod Quine). Defer until someone needs it.
2. **`--output json` as global flag** — currently `--json` is per-command where tabular is the default. A global flag may be cleaner once all commands support JSON. Revisit after v1.
3. **`modok ingest-code-map`** — code map ingestion (file/module discovery from source tree). Referenced in `project-setup.md` but the ingestion pipeline only handles docs today. Separate command when code map ingestion is built.
4. **`modok find-issue`** — look up a CustomerIssue node ID by source + ticket without running retrieval. Not needed given `retrieve` now accepts `--source` + `--ticket` directly.

## References

- `docs/setup.md` — platform bootstrap (Quine, LLM backend, `modok` install)
- `docs/project-setup.md` — expected per-project CLI invocations from a user perspective
- `docs/high-level-design.md` — CLI/MCP component description
- `docs/llds/ingestion-pipeline.md` — `run_ingestion` entry point
- `docs/llds/diagnostic-retrieval-engine.md` — `retrieve` entry point
- `docs/llds/quine-client.md` — `QuineClient`, `ping()`
