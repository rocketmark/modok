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

modok init     --project <slug> --repo <path>
modok ingest   --project <slug> [--fix] <path>
modok retrieve --project <slug> --source <system> --ticket <id>
               [--node-id <int>]
modok recall   --project <slug> --feature <feature-slug> [--json]
modok quine    (start | stop | status)
```

### `modok init`

Initializes a project in MODOK:
1. Verifies `--repo <path>` contains a `.git/` directory; exits `1` with "not a git repository: `<path>`" if absent. A repo without `.git/` is almost certainly a wrong path — the hook is a core part of init, and silently skipping it would leave the project half-initialized.
2. Validates `<repo>/registries/features.yml`, `modules.yml`, `errors.yml` exist; creates stubs if missing.
3. Installs a post-commit git hook in the project repo (delegates to `modok.ingestion.hook`).
4. Registers the project in `~/.modok/config.toml` under `[[projects]]` if not already present.

Does **not** run ingestion. Does **not** require Quine to be running.

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

Returns everything MODOK knows about a feature slug: associated docs, modules, source files, known issues, and risks. Read-only graph traversal; not tied to a customer issue.

1. Pings Quine; exits `2` if unreachable.
2. Traverses the graph from `Feature {project_slug, feature_slug}` along outbound edges.
3. Prints a human-readable tabular summary to stdout by default; `--json` emits JSON.

Exit codes: `0` on success (including when the feature slug produces no results — empty results are valid, not an error), `1` if the feature slug argument is malformed or the project is not in config, `2` if Quine is unreachable.

### `modok quine start | stop | status`

Lifecycle convenience wrapper around the Quine JAR process. Does not require `--project`.

- **`start`**: pings Quine first; if it responds, prints "Quine is already running at `<url>`" to stderr and exits `0` — no PID file check, no process launch. If not responding, validates the JAR path from config immediately and exits `1` with "Quine JAR not found at `<path>`" if absent. Then launches `java -Dconfig.file=~/.modok/quine.conf -jar ~/.modok/quine.jar` as a background process, writes PID to `~/.modok/quine.pid`, and polls `ping()` until ready (up to 30s). Exits `2` if startup times out.
- **`stop`**: reads `~/.modok/quine.pid`, sends SIGTERM, waits up to 10s for the process to exit, removes the PID file. Exits `1` if no PID file exists.
- **`status`**: calls `ping()`; prints `running` or `stopped` to stdout. Always exits `0`.

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
| JAR path validation on `quine start` | Check before forking, exit `1` with clear message | Let JVM error surface | JVM errors for missing JARs are unactionable. A path check before fork gives an operator-readable error. |

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
