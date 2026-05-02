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

modok init     --project <slug> --repo <path> [--assisted]
modok ingest   --project <slug> [--fix] <path>
modok retrieve --project <slug> --source <system> --ticket <id>
               [--node-id <int>]
modok recall   --project <slug> (--feature <slug> | --module <slug>) [--json]
modok search   --project <slug> (QUERY | --section <str> | --text <str>) [--json]
modok diagnose --project <slug> --feature <slug>
               [--error <slug>] [--symptom <str>] [--json]
modok quine    (start | stop | status)
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

Runs the ingestion pipeline over `<path>` for `--project <slug>`:
1. Pings Quine; exits `2` if unreachable.
2. Loads registries from `{repo_root}/registries/`.
3. Calls `run_ingestion(repo_root, registry, client, project_slug, fix_mode)`.
4. Prints the structured ingestion report to stdout.
5. Exits `3` if the report contains errors; `0` otherwise.

With `--fix`: invokes the LLM proposal pass for docs with missing required fields. Prompts interactively on stderr for approval. When stdout is not a tty (piped), auto-rejects proposals and emits a warning to stderr — ingestion continues without LLM proposals.

### `modok retrieve`

Fetches a debug packet for a customer issue and prints it as JSON to stdout.

**Primary form** (for agents and humans):
```
modok retrieve --project <slug> --source <system> --ticket <id>
```
Computes the Quine node ID internally via `idFrom("customer-issue", project_slug, source_system, ticket_id)`, then calls `retrieve(node_id, project_slug, client)`.

**Power-user form** (when node ID is already known):
```
modok retrieve --project <slug> --node-id <int>
```
Skips the `idFrom` computation and calls `retrieve` directly with the supplied integer.

`--source` + `--ticket` and `--node-id` are mutually exclusive. Supplying both or neither exits `1`.

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

## Non-Interactive Detection

`modok ingest --fix` and any future command that prompts the user detects non-interactive mode via `sys.stdin.isatty()`. When non-interactive, prompts are suppressed, proposals are auto-rejected, and a warning is emitted to stderr. This allows `--fix` to be used in CI or agent pipelines without hanging.

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
        retrieve.py
        recall.py
        search.py
        diagnose.py
        quine.py
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
| `retrieve` input | `--source` + `--ticket` (primary); `--node-id` (power user) | Node ID only; source+ticket only | Agents can't call `idFrom` directly — they have source system and ticket ID from their context. Node ID form retained for power users and testing. |
| Config location | `~/.modok/config.toml` fixed | `.modok.toml` in cwd, `$MODOK_CONFIG` env var | Fixed location makes agent subprocess calls predictable without path coordination. |
| Config parsing | stdlib `tomllib` + pydantic | `tomli` backport, `tomlkit` | `tomllib` is in stdlib from Python 3.11 (already required). No extra dependency. |
| Non-interactive `--fix` | Auto-reject, warn, continue | Block and fail | CI and agent pipelines can use `--fix` safely without hanging; explicit `--auto-approve` is the v2 opt-in. |
| `quine start` when already running | Ping first; if up, print and exit `0` | Check PID file; error if no PID | Quine may have been started externally. "It's already running" is success, not an error, regardless of how it started. |
| `recall` on unknown feature slug | Exit `0` with empty results | Exit `1` | "No results" is a valid graph query answer. Agents can handle empty JSON; they can't easily distinguish a real error from a missing feature if both return non-zero. |
| `init` on non-git directory | Exit `1` immediately | Skip hook, warn, continue | The hook is a core deliverable of `init`. A missing `.git/` is almost certainly a wrong path; a silent skip would leave the project half-initialized with no visible signal. |
| `init --assisted` write behaviour | Write files directly; user edits if needed | Interactive review before write | Simpler. Registry files are source-of-truth text files — editing them is the natural correction mechanism. An interactive approval loop adds ceremony with no safety benefit here. |
| JAR path validation on `quine start` | Check before forking, exit `1` with clear message | Let JVM error surface | JVM errors for missing JARs are unactionable. A path check before fork gives an operator-readable error. |
| `quine stop` when process already dead | Exit `2` with crash message, leave PID file | Treat as success (delete PID, exit `0`) | Crashes should be visible. Silently cleaning up a dead-process PID file hides the fact that Quine crashed between start and stop. The operator needs to check logs. |

## Open Questions & Future Decisions

### Deferred
1. **`$MODOK_CONFIG` env var** — config path override for multi-config setups (e.g. staging vs. prod Quine). Defer until someone needs it.
2. **`--output json` as global flag** — currently `--json` is per-command where tabular is the default. A global flag may be cleaner once all commands support JSON. Revisit after v1.
3. **`modok ingest-code-map`** — code map ingestion (file/module discovery from source tree). Referenced in `setup.md` but the ingestion pipeline only handles docs today. Separate command when code map ingestion is built.
4. **`--auto-approve` flag for `--fix`** — explicit opt-in for CI pipelines that want LLM proposals without prompts. Defer until there is a real CI use case.
5. **`modok find-issue`** — look up a CustomerIssue node ID by source + ticket without running retrieval. Not needed given `retrieve` now accepts `--source` + `--ticket` directly.

## References

- `docs/setup.md` — expected CLI invocations from a user perspective
- `docs/high-level-design.md` — CLI/MCP component description
- `docs/llds/ingestion-pipeline.md` — `run_ingestion` entry point
- `docs/llds/diagnostic-retrieval-engine.md` — `retrieve` entry point
- `docs/llds/quine-client.md` — `QuineClient`, `ping()`
