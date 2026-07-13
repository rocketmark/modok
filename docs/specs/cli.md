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

## `modok --status`

- [x] **CLI-STAT-001** [U]: When `--status` is passed, the system shall print whether Quine is reachable, including the configured URL.
- [x] **CLI-STAT-002** [U]: When Quine is reachable, the system shall print the total node count and a per-type breakdown; nodes with a null `node_type` shall be shown as `(untyped)`.
- [x] **CLI-STAT-003** [U]: When Quine is not reachable, the system shall print "not reachable at `<url>`" and omit the node count section.
- [x] **CLI-STAT-004** [U]: The system shall print the list of configured projects (slug and repo path) regardless of Quine reachability.
- [x] **CLI-STAT-005** [U]: `modok --status` shall exit `0` whether or not Quine is reachable.

---

## Config Loading

- [x] **CLI-CFG-001** [U]: The system shall read config from `~/.modok/config.toml` on every command invocation, expanding `~` in path values via `Path.expanduser()`.
- [x] **CLI-CFG-002** [U]: When `~/.modok/config.toml` does not exist, the system shall exit `1` with a message directing the user to the setup guide.
- [x] **CLI-CFG-003** [U]: When `~/.modok/config.toml` exists but is not valid TOML or fails pydantic schema validation, the system shall exit `1` with a message identifying the parse error.
- [x] **CLI-CFG-004** [U]: When `--project <slug>` names a project not present in the `[[projects]]` list in config, the system shall exit `1` with the message "project `<slug>` not found in config".

---

## Quine Startup Check

- [x] **CLI-PING-001** [U]: When any graph-touching command (`ingest`, `retrieve`, `recall`, `search`, `diagnose`) is invoked and `QuineClient.ping()` returns `False`, the system shall exit `2` with the message "Quine is not reachable at `<url>` — run `modok quine start` or check your config" without calling any graph operation.

---

## `modok init`

- [x] **CLI-INIT-001** [U]: When `--repo <path>` does not contain a `.git/` directory, `modok init` shall exit `1` with the message "not a git repository: `<path>`".
- [x] **CLI-INIT-002** [U]: When `--assisted` is not passed and `<repo>/registries/features.yml`, `modules.yml`, or `errors.yml` are missing, `modok init` shall create stub files for each missing registry and report each creation to stdout. When `--assisted` is passed, `modok init` shall not create stub files — the proposal engine is responsible for writing all registry files.
- [x] **CLI-INIT-003** [U]: `modok init` shall install a post-commit git hook in the project repo by delegating to `modok.ingestion.hook.install_post_commit_hook`.
- [x] **CLI-INIT-004** [U]: When the project slug is already present in `~/.modok/config.toml`, `modok init` shall not add a duplicate `[[projects]]` entry.
- [x] **CLI-INIT-005** [U]: When the project slug is not present in `~/.modok/config.toml`, `modok init` shall append a `[[projects]]` entry with the supplied slug and repo path.
- [x] **CLI-INIT-006** [U]: `modok init` shall not call `QuineClient.ping()` and shall not require Quine to be running.
- [x] **CLI-INIT-007** [U]: When `~/.modok/config.toml` does not exist, `modok init` shall create it with a minimal valid structure before appending the `[[projects]]` entry.
- [ ] **CLI-INIT-008** [U]: When `--assisted` is passed, `modok init` shall delegate to `modok.registry.proposal.propose_registries` after validating the repo path and before installing the git hook.
- [ ] **CLI-INIT-009** [U]: When `--assisted` is passed and the LLM gateway is unreachable, `modok init` shall exit `2` with the message "LLM gateway is not reachable — start Ollama or check `local_endpoint` in config".
- [ ] **CLI-INIT-010** [U]: When `--assisted` is not passed, `modok init` shall not invoke the LLM gateway under any circumstance.
- [ ] **CLI-INIT-011** [U]: When `--assisted` completes successfully, `modok init` shall print a summary to stdout stating the number of sections processed, docs processed, and entries written per registry file.

---

## `modok ingest`

- [x] **CLI-INGEST-001** [U]: `modok ingest` shall print the `IngestionReport` returned by `run_ingestion` to stdout.
- [x] **CLI-INGEST-002** [U]: When the ingestion report contains no errors, `modok ingest` shall exit `0`.
- [x] **CLI-INGEST-003** [U]: When the ingestion report contains one or more errors, `modok ingest` shall exit `3`.
- [x] **CLI-INGEST-004** [U]: When `--fix` is specified and `sys.stdin.isatty()` returns `False` (non-interactive), the system shall pass `fix_mode=False` to `run_ingestion` and emit a warning to stderr stating that LLM proposals were suppressed.
- [x] **CLI-INGEST-005** [U]: `modok ingest` shall derive `repo_root` from the project's `repo` path in config and load registries from `{repo_root}/registries/`.
- [x] **CLI-INGEST-006** [U]: When `--fix --strict` is specified, the system shall pass `strict=True` to `run_ingestion`; any doc with a rejected field after repair shall produce zero nodes written and a structured error in the ingestion report.
- [x] **CLI-INGEST-007** [U]: When `--fix --dry-run` is specified, the system shall pass `dry_run=True` to `run_ingestion`; no files or Quine nodes shall be written and the command shall exit `0`.
- [x] **CLI-INGEST-008** [U]: When `--fix --emit-counterexamples` is specified, the system shall pass `emit_counterexamples=True` to `run_ingestion`; a YAML counterexample file shall be written to `{repo_root}/tests/fixtures/llm_gateway/` for each doc with rejected fields.
- [x] **CLI-INGEST-009** [U]: `--strict`, `--dry-run`, and `--emit-counterexamples` shall be valid only when `--fix` is also specified; supplying any of them without `--fix` shall exit `1` with a usage error.

---

## `modok retrieve`

- [x] **CLI-RET-001** [U]: When `--ticket <id>` is supplied, `modok retrieve` shall look up the `CustomerIssue` node via `MATCH (n) WHERE n.project_slug = $p AND n.ticket_id = $t RETURN id(n)` and call `retrieve(node_id, project_slug, client)`. If no node is found, it shall exit `1` with a usage error.
- [x] **CLI-RET-002** [U]: When `--node-id <int>` is supplied, `modok retrieve` shall call `retrieve(node_id, project_slug, client)` directly without performing a graph lookup.
- [x] **CLI-RET-003** [U]: When both `--ticket` and `--node-id` are supplied, `modok retrieve` shall exit `1` with a usage error before any other validation or graph operation.
- [x] **CLI-RET-004** [U]: When neither `--ticket` nor `--node-id` are supplied, `modok retrieve` shall exit `1` with a usage error.
- [x] **CLI-RET-006** [U]: When `retrieve` raises `DRENotFoundError`, `modok retrieve` shall exit `1` with the message "issue not found in project `<slug>`".
- [x] **CLI-RET-007** [U]: When `retrieve` raises `DREGraphUnavailableError`, `modok retrieve` shall exit `2`.
- [x] **CLI-RET-008** [U]: When `retrieve` raises `DRELLMUnavailableError`, `modok retrieve` shall exit `2`.
- [x] **CLI-RET-009** [U]: On success, `modok retrieve` shall print the `DebugPacket` serialized as JSON to stdout and exit `0`.

---

## `modok recall`

- [x] **CLI-REC-001** [U]: When `--feature <slug>` is supplied, `modok recall` shall traverse from the matching Feature node along all outbound edges and include the feature node and all directly connected nodes in the result.
- [x] **CLI-REC-002** [U]: When `--module <slug>` is supplied, `modok recall` shall traverse from the matching Module node, resolving its implementing feature and source files, and include all returned nodes in the result.
- [x] **CLI-REC-003** [U]: When neither `--feature` nor `--module` is supplied, `modok recall` shall exit `1` with a usage error before any graph operation.
- [x] **CLI-REC-004** [U]: When both `--feature` and `--module` are supplied, `modok recall` shall run both traversals and deduplicate results by node ID before printing.
- [x] **CLI-REC-005** [U]: When a query produces no graph results, `modok recall` shall print an empty result and exit `0`.
- [x] **CLI-REC-006** [U]: Without `--json`, `modok recall` shall print results in a human-readable tabular format to stdout, formatted per node type.
- [x] **CLI-REC-007** [U]: With `--json`, `modok recall` shall print results as a JSON object to stdout.
- [x] **CLI-REC-008** [U]: When Quine is unreachable during `modok recall`, the system shall exit `2`.
- [x] **CLI-REC-009** [U]: A returned row whose node has no `node_type` property (a Quine address referenced by `idFrom()` but never actually written — e.g. `--module <slug>` for a module that was never ingested) shall be excluded from the result, so a nonexistent slug prints "(no results)" rather than a misleading empty `[Node] {}`.

---

## `modok quine start`

- [x] **CLI-QS-001** [U]: When `QuineClient.ping()` returns `True` before any launch attempt, `modok quine start` shall print "Quine is already running at `<url>`" to stderr and exit `0` without spawning a process.
- [x] **CLI-QS-002** [U]: When the JAR path from config does not exist on disk, `modok quine start` shall exit `1` with the message "Quine JAR not found at `<path>` — run the setup guide to download it" without spawning a process.
- [x] **CLI-QS-003** [U]: When the JAR path exists and Quine is not already running, `modok quine start` shall launch the Quine JAR as a background process and write its PID to `~/.modok/quine.pid`.
- [x] **CLI-QS-004** [U]: When Quine does not become reachable within 30 seconds of launch, `modok quine start` shall exit `2` with the message "Quine did not become ready within 30s".
- [x] **CLI-QS-005** [U]: When Quine becomes reachable within 30 seconds of launch, `modok quine start` shall exit `0`.

---

## `modok quine stop`

- [x] **CLI-QSTOP-001** [U]: When `~/.modok/quine.pid` does not exist, `modok quine stop` shall exit `1` with the message "Quine is not running (no PID file found)".
- [x] **CLI-QSTOP-002** [U]: When the PID file exists but the process is not running (ESRCH — process already dead), `modok quine stop` shall exit `2` with the message "process `<pid>` not found — Quine may have crashed; check `~/.modok/quine.log`" and leave the PID file in place.
- [x] **CLI-QSTOP-003** [U]: When the process exits within 10 seconds of receiving SIGTERM, `modok quine stop` shall remove `~/.modok/quine.pid` and exit `0`.
- [x] **CLI-QSTOP-004** [U]: When the process does not exit within 10 seconds of receiving SIGTERM, `modok quine stop` shall exit `2` with the message "Quine did not stop within 10s — PID file left in place".

---

## `modok quine status`

- [x] **CLI-QSTAT-001** [U]: When `QuineClient.ping()` returns `True`, `modok quine status` shall print `running` to stdout and exit `0`.
- [x] **CLI-QSTAT-002** [U]: When `QuineClient.ping()` returns `False`, `modok quine status` shall print `stopped` to stdout and exit `0`.

---

## `modok search`

- [x] **CLI-SRCH-001** [U]: When a bare `QUERY` argument is supplied, `modok search` shall treat it as shorthand for `--text <QUERY>` and perform a full-property substring search.
- [x] **CLI-SRCH-002** [U]: When both a bare `QUERY` argument and `--text` are supplied, `modok search` shall exit `1` with a usage error before any graph operation.
- [x] **CLI-SRCH-003** [U]: When neither a bare `QUERY`, `--section`, nor `--text` is supplied, `modok search` shall exit `1` with a usage error.
- [x] **CLI-SRCH-004** [U]: When `--section <str>` is supplied, `modok search` shall query only `DocSection` nodes whose `heading_text` contains the search string (case-insensitive), ordered by `doc_path` then `line_start`.
- [x] **CLI-SRCH-005** [U]: When `--text <str>` (or bare `QUERY`) is supplied, `modok search` shall query all node types, matching case-insensitively against `heading_text`, `name`, `summary`, `normalized_error`, `module_slug`, `feature_slug`, and `repo_path`.
- [x] **CLI-SRCH-006** [U]: When both `--section` and a text query (`--text` or bare `QUERY`) are supplied, `modok search` shall run both queries and deduplicate results by node ID before printing.
- [x] **CLI-SRCH-007** [U]: When the search produces no results, `modok search` shall print "(no results)" and exit `0`.
- [x] **CLI-SRCH-008** [U]: Without `--json`, `modok search` shall print results in a human-readable tabular format to stdout, formatted per node type.
- [x] **CLI-SRCH-009** [U]: With `--json`, `modok search` shall print results as `{"project": "<slug>", "nodes": [...]}` to stdout.
- [x] **CLI-SRCH-010** [U]: When Quine is unreachable during `modok search`, the system shall exit `2`.

---

## `modok diagnose`

- [x] **CLI-DIAG-001** [U]: When `--feature <slug>` is not supplied, `modok diagnose` shall exit `1` with a usage error before any graph operation.
- [x] **CLI-DIAG-002** [U]: `modok diagnose` shall traverse `Feature -[:IMPLEMENTED_BY]-> Module -[:DEFINED_IN]-> File` and include all matching `File` nodes in `relevant_files`.
- [x] **CLI-DIAG-003** [U]: `modok diagnose` shall traverse `Feature -[:HAS_KNOWN_ISSUE]-> KnownIssue` and include matching `KnownIssue` nodes in `known_issues`.
- [x] **CLI-DIAG-004** [U]: When `--symptom <str>` is supplied, `modok diagnose` shall include only `KnownIssue` nodes whose `summary` contains the substring (case-insensitive); `KnownIssue` nodes that do not match shall be excluded from `known_issues`.
- [x] **CLI-DIAG-005** [U]: When `--error <slug>` is supplied, `modok diagnose` shall fetch the `ErrorSignature` node with `normalized_error = <slug>` and traverse `KnownIssue -[:HAS_ERROR]-> ErrorSignature` to find additional `KnownIssue` nodes; each found this way that passes the `--symptom` filter (if any) shall have its `match_count` incremented by 1.
- [x] **CLI-DIAG-006** [U]: For each `KnownIssue` in the result set, `modok diagnose` shall traverse `KnownIssue -[:RESOLVED_BY]-> Fix` and include matching `Fix` nodes in `recent_fixes`.
- [x] **CLI-DIAG-007** [U]: Results shall be deduplicated by node ID; a `KnownIssue` reachable via both the feature edge and the error signature traversal shall appear once with `match_count = 2`.
- [x] **CLI-DIAG-008** [U]: Each result list shall be sorted descending by `match_count`.
- [x] **CLI-DIAG-009** [U]: The output shall use the `DebugPacket` schema: `anchors.feature_slugs` set from `--feature`; `anchors.error_signatures` from `--error` if given; `anchors.symptoms` from `--symptom` if given; `issue_summary` set to `"diagnose: <feature_slug>"`; `confidence` set to `1.0`.
- [x] **CLI-DIAG-010** [U]: When the traversal produces no results, `modok diagnose` shall print an empty packet and exit `0`.
- [x] **CLI-DIAG-011** [U]: Without `--json`, `modok diagnose` shall print results in a human-readable tabular format to stdout.
- [x] **CLI-DIAG-012** [U]: With `--json`, `modok diagnose` shall print the `DebugPacket` serialized as JSON to stdout.
- [x] **CLI-DIAG-013** [U]: When Quine is unreachable during `modok diagnose`, the system shall exit `2`.
