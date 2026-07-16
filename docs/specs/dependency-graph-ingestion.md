# Dependency-Graph Ingestion Specs

See `docs/llds/dependency-graph-ingestion.md`. Test Level Convention matches `docs/specs/quine-client.md § Test Level Convention`: `[U]` unit-testable against a mocked client; `[C]` requires confirmation against a live Quine instance; `[P]` a property that must hold regardless of ordering/repetition.

---

## Version-Fidelity Source Priority

- [ ] **DEPG-SRC-001** [U]: WHEN resolving the version identity for a detected package change, THE SYSTEM SHALL try, in order: (1) the GitHub dependency-review compare API response for that manifest/package, (2) — if (1) did not resolve it and the PR is Dependabot-authored — the PR title's `Bump {package} from {old} to {new}` pattern or pip's `Update {package} requirement from {old} to {new} in {path}` pattern (Dependabot's title wording varies by ecosystem/updater, not just by package), (3) the raw manifest-declared text already captured by the snapshot diff. The first source that resolves a value SHALL be used; later sources SHALL NOT be consulted once one has.
- [ ] **DEPG-SRC-002** [U]: IF the dependency-review compare API returns a non-2xx response (403, 404, 422, or 5xx) for a PR, THEN THE SYSTEM SHALL treat source 1 as unavailable for that PR, log it once, and fall through to source 2/3 — without aborting the current poll cycle or any other PR's processing.
- [ ] **DEPG-SRC-003** [U]: Dependabot title parsing (source 2) SHALL only be attempted for PRs where `user.login == "dependabot[bot]"` (the same detection `docs/llds/github-ingestion.md § Field mapping — Fix` already uses) AND source 1 did not resolve the package in question. A grouped Dependabot update, whose title does not enumerate individual package versions, SHALL fall through to source 3 for each package in the group.
- [ ] **DEPG-SRC-004** [U]: Detection of *whether* a package's declared version changed SHALL NOT depend on source 1 or source 2 being available — it is answered solely by the snapshot text diff (§ Snapshot Diffing). Source 1/2 availability SHALL only affect the precision of the `DependencyVersion.version`/`relationship` values recorded for an already-detected change.

## Node Types

- [ ] **DEPG-NODE-001** [U]: The system shall address `DependencyPackage` nodes with `idFrom('dependency-package', project_slug, purl)`, carrying `purl`, `ecosystem`, `name`.
- [ ] **DEPG-NODE-002** [U]: The system shall address `DependencyVersion` nodes with `idFrom('dependency-version', project_slug, package_purl, version)`, carrying `package_purl`, `version`, `relationship` (one of `"direct"`, `"transitive"`, `"unknown"`).
- [ ] **DEPG-NODE-003** [U]: The system shall address `DependencyManifest` nodes with `idFrom('dependency-manifest', project_slug, manifest_path)`, carrying `manifest_path`, `ecosystem`, `format`.
- [ ] **DEPG-NODE-004** [U]: The system shall address `DependencySnapshot` nodes with `idFrom('dependency-snapshot', project_slug, manifest_path, commit_sha)`, where `commit_sha` is always the PR's `merge_commit_sha` — never `head.sha` — carrying `manifest_path`, `commit_sha`, `captured_at`.
- [ ] **DEPG-NODE-005** [U]: The system shall address `DependencyChange` nodes with `idFrom('dependency-change', project_slug, manifest_path, package_purl, commit_sha)`, carrying `change_kind` (`"added"` | `"removed"` | `"changed"`), `version_source` (`"dependency_review"` | `"dependabot_title"` | `"manifest_diff"`), `observed_at`.
- [ ] **DEPG-NODE-006** [U]: The system shall NOT introduce a `PullRequest` node type. Merged-PR provenance for a `DependencyChange` SHALL be represented via `MERGED_VIA` to the existing `Fix` node (`idFrom('fix', project_slug, fix_id)`, per `docs/llds/github-ingestion.md`).

## Edges

- [ ] **DEPG-EDGE-001** [U]: The system shall write `DependencyVersion -[:VERSION_OF]-> DependencyPackage` once, at `DependencyVersion` creation, and never modify or remove it afterward.
- [ ] **DEPG-EDGE-002** [U]: The system shall write one `DependencySnapshot -[:CONTAINS]-> DependencyVersion` edge per package declared in that snapshot's manifest content, written once at snapshot creation and never removed.
- [ ] **DEPG-EDGE-003** [U]: The system shall write `DependencySnapshot -[:FOR_COMMIT]-> Commit` only when `node_exists_by_parts(("commit", project_slug, commit_sha))` — silently skipped, not retried inline, if the `Commit` node does not yet exist (§ Reconciliation).
- [ ] **DEPG-EDGE-004** [U]: For a `DependencyChange` with `change_kind == "added"`, THE SYSTEM SHALL write `CHANGED_PACKAGE` and `TO_VERSION` only (no `FROM_VERSION`). For `change_kind == "removed"`, THE SYSTEM SHALL write `CHANGED_PACKAGE` and `FROM_VERSION` only (no `TO_VERSION`). For `change_kind == "changed"`, THE SYSTEM SHALL write all three.
- [ ] **DEPG-EDGE-005** [U]: The system shall write `DependencyChange -[:INTRODUCED_BY]-> Commit` and `DependencyChange -[:MERGED_VIA]-> Fix` only when the respective target node already exists (`node_exists_by_parts`) — never inventing a `Commit` or `Fix` node to satisfy this edge.
- [ ] **DEPG-EDGE-006** [U]: WHEN a new `DependencySnapshot` is written for a manifest, THE SYSTEM SHALL reconcile `DependencyManifest -[:DECLARES]-> DependencyVersion` (via `replace_edges_by_parts`) to point at exactly that snapshot's `CONTAINS` targets, replacing whatever it pointed at before.
- [ ] **DEPG-EDGE-007** [U]: WHEN file-usage mapping runs for a `File` (§ File Usage Mapping), THE SYSTEM SHALL reconcile that `File`'s `USES_DEPENDENCY` edges (via `replace_edges_by_parts`) to exactly the set of `DependencyPackage` nodes resolved from its current imports — a package no longer imported SHALL lose its edge on the next run.

## Tracked-Manifest Detection

- [ ] **DEPG-DETECT-001** [U]: The system shall recognize a file as a tracked manifest when its filename matches the static ecosystem table (§ Tracked-Manifest Detection in the LLD) — `requirements*.txt` and `pyproject.toml` mapped to `pypi` and parsed; `Pipfile`/`poetry.lock`/`uv.lock` (pypi), `package.json`/`package-lock.json`/`yarn.lock` (npm), `*.csproj`/`packages.config`/`Directory.Packages.props` (nuget), `Gemfile`/`Gemfile.lock` (rubygems), `go.mod`/`go.sum` (go), `Cargo.toml`/`Cargo.lock` (cargo) recognized but not parsed in v1.
- [ ] **DEPG-DETECT-002** [U]: WHEN a merged PR touches a manifest path whose ecosystem is "detected only" (not yet parsed), THE SYSTEM SHALL upsert a `DependencyManifest` node for it but SHALL NOT write a `DependencySnapshot` or `DependencyChange` from its content.
- [ ] **DEPG-DETECT-003** [U]: WHERE a project's config sets `dependency_manifest_globs`, THE SYSTEM SHALL narrow tracked-manifest matches to paths also matching at least one glob. WHERE it is unset (default), THE SYSTEM SHALL track every statically-detected manifest path in the diff.

## Manifest Parsing (v1: pypi)

- [ ] **DEPG-PARSE-001** [U]: For a `requirements*.txt`-format file, THE SYSTEM SHALL parse each non-comment, non-blank, non-directive (`-r`, `-e`, `--index-url`, ...) line as `name{comparator}version[,comparator version]*[; marker]`, recording the full comparator+version string (not just the numeric version) as the declared value.
- [ ] **DEPG-PARSE-002** [U]: For `pyproject.toml`, THE SYSTEM SHALL parse only the PEP 621 `[project.dependencies]` array, using the same per-entry format as DEPG-PARSE-001. `[tool.poetry.dependencies]` SHALL be recognized as present (the file is still a tracked manifest) but SHALL NOT be parsed in v1.
- [ ] **DEPG-PARSE-003** [U]: IF a manifest line does not match the expected format, THEN THE SYSTEM SHALL skip and log that line (`parse_error`) and continue parsing the remaining lines — a single malformed line SHALL NOT abort parsing of the rest of the file.
- [ ] **DEPG-PARSE-004** [U]: Before constructing a `purl`, THE SYSTEM SHALL PEP-503-normalize the package name (lowercase; runs of `-`, `_`, `.` collapsed to a single `-`), so that `PySide6`, `pyside6`, and `PySide-6` all resolve to the same `DependencyPackage`.

## Snapshot Diffing

- [ ] **DEPG-DIFF-001** [U]: WHEN fetching manifest content to snapshot, THE SYSTEM SHALL fetch it at the PR's `merge_commit_sha` via the Contents API — never at `head.sha` (the PR branch's own tip commit, which for a squash or rebase merge may not exist in default-branch history at all).
- [ ] **DEPG-DIFF-002** [U]: To find the prior snapshot for a manifest, THE SYSTEM SHALL query `DependencySnapshot` nodes filtered by `project_slug` and `manifest_path`, ordered by `captured_at` descending with `commit_sha` descending as a tiebreaker, taking the first result before the current snapshot's `captured_at`.
- [ ] **DEPG-DIFF-003** [U]: WHEN a manifest has no prior `DependencySnapshot` (this is the first one observed for that manifest), THE SYSTEM SHALL write the snapshot and its `CONTAINS` edges but SHALL NOT write any `DependencyChange` records — every package in a manifest's first tracked snapshot is "first observed," not "changed."
- [ ] **DEPG-DIFF-004** [P]: Given a prior and a new snapshot's package sets, THE SYSTEM SHALL classify each package present in the new set only as `"added"`, each present in the prior set only as `"removed"`, each present in both with a different declared-text value as `"changed"`, and each present in both with an identical declared-text value as no change (no `DependencyChange` written) — regardless of iteration order over the two sets.
- [ ] **DEPG-DIFF-005** [P]: Reprocessing the same merged PR (retry, or a duplicate poll cycle) SHALL produce exactly the same `DependencySnapshot` and `DependencyChange` nodes/edges as the first processing — no duplicates, via `upsert_node`/`write_edge_by_parts`'s existing idempotency on deterministic keys.

## Polling and Checkpoint Behavior

- [ ] **DEPG-POLL-001** [U]: The system shall maintain `last_dependency_sync` as a per-project cursor in `~/.modok/config.toml`, independent of `last_github_sync` and `last_workflow_sync`.
- [ ] **DEPG-POLL-002** [U]: Each poll cycle, THE SYSTEM SHALL fetch merged PRs with `updated_at > last_dependency_sync` (empty cursor = fetch all, matching `last_github_sync`'s existing semantics) and process them in ascending `updated_at` order.
- [ ] **DEPG-POLL-003** [U]: THE SYSTEM SHALL advance `last_dependency_sync` to a given PR's own `updated_at` only immediately after that PR's dependency processing completes (successfully, or as a clean no-manifest-touched no-op) — not after the whole fetched batch.
- [ ] **DEPG-POLL-004** [U]: IF processing PR *N* fails, THEN THE SYSTEM SHALL leave `last_dependency_sync` at PR *N-1*'s `updated_at`, log the failure, and continue to the next PR in this cycle's batch without aborting — PR *N* SHALL be refetched and retried on a later cycle since the cursor did not advance past it.
- [ ] **DEPG-POLL-005** [U]: WHEN a merged PR's changed-files list contains no path matching a tracked manifest, THE SYSTEM SHALL treat it as a no-op (no `DependencySnapshot`/`DependencyChange` writes) and still advance the cursor past it (DEPG-POLL-003).
- [ ] **DEPG-POLL-006** [U]: A failure in dependency-cycle processing (this step) SHALL NOT prevent issue/PR sync or CI ingestion from running in the same poll cycle, and vice versa — each step's `try`/`except` is independent (matching the existing isolation between issue/PR sync and CI ingestion in `_poll_once`).

## Reconciliation

- [ ] **DEPG-RECON-001** [U, P]: Once per poll cycle, per project, independent of `last_dependency_sync`, THE SYSTEM SHALL sweep `DependencyChange` nodes missing an `INTRODUCED_BY` or `MERGED_VIA` edge whose target (`Commit` or `Fix`, respectively) now exists, and write the missing edge(s) (`write_edge_by_parts`) — this sweep SHALL NOT depend on any cursor value.

## File Usage Mapping

- [ ] **DEPG-USAGE-001** [U]: The system shall run file-to-dependency-usage mapping once per `modok ingest` invocation, after the existing per-doc write loop in `run_ingestion` completes.
- [ ] **DEPG-USAGE-002** [U]: File-usage mapping SHALL only process a code-map entry with `role: source` and `language: python` whose path already has an existing `File` node (`node_exists_by_parts`) — it SHALL NOT create a `File` node itself.
- [ ] **DEPG-USAGE-003** [U]: For each import, THE SYSTEM SHALL take the top-level module name and skip it if it is a standard-library module (per `sys.stdlib_module_names`) — standard-library imports SHALL NOT produce a `USES_DEPENDENCY` edge or a `DependencyPackage` lookup.
- [ ] **DEPG-USAGE-004** [U]: WHERE `.modok/dependency-map.yml` declares an `import_overrides` entry for an import's top-level module name, THE SYSTEM SHALL use the override's package name. WHERE no override exists, THE SYSTEM SHALL use the import's top-level module name directly as the package name (identity mapping).
- [ ] **DEPG-USAGE-005** [U]: A resolved package SHALL only be added to a `File`'s `USES_DEPENDENCY` target set when a `DependencyPackage` node with the corresponding `purl` already exists (`node_exists_by_parts`) — a package with no known manifest declaration SHALL NOT get an edge, and SHALL NOT cause a `DependencyPackage` node to be invented.
- [ ] **DEPG-USAGE-006** [U]: File-usage mapping SHALL only run for `File` nodes (not `TestFile`) and Python imports, in v1.

## Existing Retrieval Integration

- [ ] **DEPG-DRE-001** [U]: The system shall add a `dependency_change` `EvidenceItem` type with a fixed score of `5.0`. It SHALL NOT be added to `_NON_CORROBORATING_TYPES`.
- [ ] **DEPG-DRE-002** [U]: `dependency_change` evidence SHALL only be added to a file path already present in `file_evidence`/`test_file_evidence` for the current retrieval — it SHALL NOT cause a new file to be added to either evidence map (matching the existing element-anchor-matching rule).
- [ ] **DEPG-DRE-003** [U]: For each anchored file path, THE SYSTEM SHALL traverse `File -[:USES_DEPENDENCY]-> DependencyPackage <-[:CHANGED_PACKAGE]- DependencyChange` (with `TO_VERSION` required and `FROM_VERSION`/`INTRODUCED_BY`/`MERGED_VIA` optional) and deduplicate results by `DependencyChange` id. This traversal SHALL NOT sort or cap results by recency.
- [ ] **DEPG-DRE-004** [U]: `DebugPacket.recent_dependency_changes` SHALL be populated on both existing `DebugPacket` construction sites in `retrieve()` — the `"partial"` `on_progress` packet and the final returned packet.
- [ ] **DEPG-DRE-005** [U]: `RecentDependencyChange.explanation` SHALL be composed via a mechanical string template (e.g. `"{package} {from} -> {to} ({manifest}), used by {files}"`) — no LLM call is made to produce it.
- [ ] **DEPG-DRE-006** [P]: For a ticket resolving to a given set of anchored files, a `DependencyChange` affecting a `DependencyPackage` that none of those files' `USES_DEPENDENCY` edges reach SHALL contribute no evidence to any candidate in that retrieval — regardless of how recently that change was observed relative to a `DependencyChange` that does reach an anchored file.

## Failure Handling

- [ ] **DEPG-ERR-001** [U]: IF `GET /pulls/{number}/files` fails (5xx/timeout) for a PR, THEN THE SYSTEM SHALL skip that PR's dependency processing for this cycle without advancing `last_dependency_sync` past it (DEPG-POLL-004).
- [ ] **DEPG-ERR-002** [U]: IF the Contents API returns 404 for a tracked manifest at `merge_commit_sha` (deleted or renamed in this PR) AND a prior `DependencySnapshot` exists for that manifest, THEN THE SYSTEM SHALL record every package in the prior snapshot as `change_kind="removed"` and SHALL NOT write a new `DependencySnapshot`.
- [ ] **DEPG-ERR-003** [U]: IF a GitHub API call in this component receives HTTP 429, THEN THE SYSTEM SHALL apply the same `Retry-After`-aware retry-once-then-exit handling `GithubIngester` already uses (`docs/llds/github-ingestion.md § Rate Limiting`).

A malformed manifest line is governed by DEPG-PARSE-003 (§ Manifest Parsing), not restated here.

## Scope Boundary

- [ ] **DEPG-SCOPE-001** [U]: Dependency-graph ingestion SHALL NOT create any `Investigation`, `InvestigationMilestone`, GitHub comment, or standing-query registration as a side effect of any node/edge write in this component.

## Open Questions & Future Decisions

Traced to `docs/llds/dependency-graph-ingestion.md § Open Questions & Future Decisions` — not independently spec'd, since none are behavioral requirements yet (lockfile parsing, grouped Dependabot title parsing, `.modok/dependency-map.yml` schema growth, non-pypi parser implementations, direct pushes without a PR, first-sync backfill cap, manifest rename handling).
