# CLI Specs

Specs for `modok.cli` — the command-line entry point over MODOK's core components.

LLD: `docs/llds/cli.md`

---

## Test Level Convention

See `docs/testing-standard.md` for full definitions.

- **[U]** — Unit test with mocked dependencies.
- **[P]** — Property test (`hypothesis`). Implies [U].
- **[C]** — Contract test against live Quine instance. Implies [U].

---

## Config Loading

- [ ] **CLI-CFG-001** [U]: The system shall read config from `~/.modok/config.toml` on every command invocation, expanding `~` in path values via `Path.expanduser()`.
- [ ] **CLI-CFG-002** [U]: When `~/.modok/config.toml` does not exist, the system shall exit `1` with a message directing the user to the setup guide.
- [ ] **CLI-CFG-003** [U]: When `~/.modok/config.toml` exists but is not valid TOML or fails pydantic schema validation, the system shall exit `1` with a message identifying the parse error.
- [ ] **CLI-CFG-004** [U]: When `--project <slug>` names a project not present in the `[[projects]]` list in config, the system shall exit `1` with the message "project `<slug>` not found in config".

---

## Quine Startup Check

- [ ] **CLI-PING-001** [U]: When any graph-touching command (`ingest`, `retrieve`, `recall`) is invoked and `QuineClient.ping()` returns `False`, the system shall exit `2` with the message "Quine is not reachable at `<url>` — run `modok quine start` or check your config" without calling any graph operation.

---

## `modok init`

- [ ] **CLI-INIT-001** [U]: When `--repo <path>` does not contain a `.git/` directory, `modok init` shall exit `1` with the message "not a git repository: `<path>`".
- [ ] **CLI-INIT-002** [U]: When `<repo>/registries/features.yml`, `modules.yml`, or `errors.yml` are missing, `modok init` shall create stub files for each missing registry and report each creation to stdout.
- [ ] **CLI-INIT-003** [U]: `modok init` shall install a post-commit git hook in the project repo by delegating to `modok.ingestion.hook.install_post_commit_hook`.
- [ ] **CLI-INIT-004** [U]: When the project slug is already present in `~/.modok/config.toml`, `modok init` shall not add a duplicate `[[projects]]` entry.
- [ ] **CLI-INIT-005** [U]: When the project slug is not present in `~/.modok/config.toml`, `modok init` shall append a `[[projects]]` entry with the supplied slug and repo path.
- [ ] **CLI-INIT-006** [U]: `modok init` shall not call `QuineClient.ping()` and shall not require Quine to be running.
- [ ] **CLI-INIT-007** [U]: When `~/.modok/config.toml` does not exist, `modok init` shall create it with a minimal valid structure before appending the `[[projects]]` entry.

---

## `modok ingest`

- [ ] **CLI-INGEST-001** [U]: `modok ingest` shall print the `IngestionReport` returned by `run_ingestion` to stdout.
- [ ] **CLI-INGEST-002** [U]: When the ingestion report contains no errors, `modok ingest` shall exit `0`.
- [ ] **CLI-INGEST-003** [U]: When the ingestion report contains one or more errors, `modok ingest` shall exit `3`.
- [ ] **CLI-INGEST-004** [U]: When `--fix` is specified and `sys.stdin.isatty()` returns `False` (non-interactive), the system shall pass `fix_mode=False` to `run_ingestion` and emit a warning to stderr stating that LLM proposals were suppressed.
- [ ] **CLI-INGEST-005** [U]: `modok ingest` shall derive `repo_root` from the project's `repo` path in config and load registries from `{repo_root}/registries/`.

---

## `modok retrieve`

- [ ] **CLI-RET-001** [U]: When `--source <system>` and `--ticket <id>` are supplied, `modok retrieve` shall compute the Quine node ID via `idFrom("customer-issue", project_slug, source_system, ticket_id)` and call `retrieve(node_id, project_slug, client)`.
- [ ] **CLI-RET-002** [U]: When `--node-id <int>` is supplied, `modok retrieve` shall call `retrieve(node_id, project_slug, client)` directly without computing an ID.
- [ ] **CLI-RET-003** [U]: When both `--source`/`--ticket` and `--node-id` are supplied, `modok retrieve` shall exit `1` with a usage error before any other validation or graph operation.
- [ ] **CLI-RET-004** [U]: When neither `--source`/`--ticket` nor `--node-id` are supplied, `modok retrieve` shall exit `1` with a usage error.
- [ ] **CLI-RET-005** [U]: When `--source` is supplied without `--ticket`, or `--ticket` is supplied without `--source`, `modok retrieve` shall exit `1` with a usage error before performing any graph operation.
- [ ] **CLI-RET-006** [U]: When `retrieve` raises `DRENotFoundError`, `modok retrieve` shall exit `1` with the message "issue not found in project `<slug>`".
- [ ] **CLI-RET-007** [U]: When `retrieve` raises `DREGraphUnavailableError`, `modok retrieve` shall exit `2`.
- [ ] **CLI-RET-008** [U]: When `retrieve` raises `DRELLMUnavailableError`, `modok retrieve` shall exit `2`.
- [ ] **CLI-RET-009** [U]: On success, `modok retrieve` shall print the `DebugPacket` serialized via `model_dump()` as JSON to stdout and exit `0`.

---

## `modok recall`

- [ ] **CLI-REC-001** [U]: `modok recall` shall print results for the named feature to stdout.
- [ ] **CLI-REC-002** [U]: When the feature slug produces no graph results, `modok recall` shall print an empty result and exit `0`.
- [ ] **CLI-REC-003** [U]: Without `--json`, `modok recall` shall print results in a human-readable tabular format to stdout.
- [ ] **CLI-REC-004** [U]: With `--json`, `modok recall` shall print results as a JSON object to stdout.
- [ ] **CLI-REC-005** [U]: When Quine is unreachable during `modok recall`, the system shall exit `2`.

---

## `modok quine start`

- [ ] **CLI-QS-001** [U]: When `QuineClient.ping()` returns `True` before any launch attempt, `modok quine start` shall print "Quine is already running at `<url>`" to stderr and exit `0` without spawning a process.
- [ ] **CLI-QS-002** [U]: When the JAR path from config does not exist on disk, `modok quine start` shall exit `1` with the message "Quine JAR not found at `<path>` — run the setup guide to download it" without spawning a process.
- [ ] **CLI-QS-003** [U]: When the JAR path exists and Quine is not already running, `modok quine start` shall launch the Quine JAR as a background process and write its PID to `~/.modok/quine.pid`.
- [ ] **CLI-QS-004** [U]: When Quine does not become reachable within 30 seconds of launch, `modok quine start` shall exit `2` with the message "Quine did not become ready within 30s".
- [ ] **CLI-QS-005** [U]: When Quine becomes reachable within 30 seconds of launch, `modok quine start` shall exit `0`.

---

## `modok quine stop`

- [ ] **CLI-QSTOP-001** [U]: When `~/.modok/quine.pid` does not exist, `modok quine stop` shall exit `1` with the message "Quine is not running (no PID file found)".
- [ ] **CLI-QSTOP-002** [U]: When the PID file exists but the process is not running (ESRCH — process already dead), `modok quine stop` shall exit `2` with the message "process `<pid>` not found — Quine may have crashed; check `~/.modok/quine.log`" and leave the PID file in place.
- [ ] **CLI-QSTOP-003** [U]: When the process exits within 10 seconds of receiving SIGTERM, `modok quine stop` shall remove `~/.modok/quine.pid` and exit `0`.
- [ ] **CLI-QSTOP-004** [U]: When the process does not exit within 10 seconds of receiving SIGTERM, `modok quine stop` shall exit `2` with the message "Quine did not stop within 10s — PID file left in place".

---

## `modok quine status`

- [ ] **CLI-QSTAT-001** [U]: When `QuineClient.ping()` returns `True`, `modok quine status` shall print `running` to stdout and exit `0`.
- [ ] **CLI-QSTAT-002** [U]: When `QuineClient.ping()` returns `False`, `modok quine status` shall print `stopped` to stdout and exit `0`.
