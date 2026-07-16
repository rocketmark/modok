# Dependency-Graph Ingestion

## Context and Design Philosophy

See `docs/high-level-design.md § GitHub Dependency-Graph Ingestion` and the corresponding falsifiable claim under § Key Design Decisions. The goal: when a dependency such as `bleak` changes version, MODOK records the package/version change, links it to the commit and PR that introduced it, maps which source files use that dependency, and lets the change surface as evidence in existing recall — not a claim of root cause, just an inspectable graph path a human or agent can follow.

This is **additional resource-type coverage on ingestion paths that already exist**, not new infrastructure:

- The existing 30-second `GitHubPollAdapter` (`docs/llds/continuous-ci-ingestion.md § Poll Cycle Extension`) gains a fourth per-cycle step, on its own cursor, for package/version/manifest/change data — same pattern CI ingestion used to add workflow-run discovery alongside issue/PR sync.
- The existing `modok ingest` path (`docs/llds/ingestion-pipeline.md`, ultimately `modok.ingestion.pipeline.run_ingestion`) gains one additional call for `File -[:USES_DEPENDENCY]-> DependencyPackage` edges, driven by the code map's already-captured, currently-unconsumed `imports` field (`docs/llds/code-map.md § Symbol Extraction`).
- `Commit` and `Fix` are reused as-is. A merged Dependabot PR is already ingested as a `Fix` with `kind="dependency-update"` (`docs/llds/github-ingestion.md § Field mapping — Fix`) — this component does not introduce a new `PullRequest` node type; `MERGED_VIA` points at the existing `Fix` node.
- The Diagnostic Retrieval Engine's recent-commit evidence path (`docs/llds/diagnostic-retrieval-engine.md § Recent commits`, § Evidence Sources) gains a sibling: recent *dependency* changes, scored the same corroborating way `element_anchor_match`/`function_anchor_match` are — never a standalone recency signal.

Two disciplines carry over unchanged from the rest of MODOK's ingestion:

**Never invent a node.** Every edge this component writes is gated on `node_exists_by_parts` for its target — a `DependencyChange` whose `Commit` or `Fix` hasn't been ingested yet (by the *separate*, local `ingest-git` path, or by an earlier poll cycle) simply doesn't get that edge yet, the same way `IMPLEMENTED_IN`/`RESOLVED_BY` already tolerate a missing `Commit`.

**MODOK is not the source of record.** It stores references (package name, version strings, commit SHAs, PR/Fix IDs) and the relationships between them — never a resolved dependency tree, never a full lockfile copy.

---

## Data Sources and Priority

Two separable questions, addressed by different mechanisms:

1. **Did a manifest change, and for which packages?** — answered deterministically, always, by diffing two `DependencySnapshot`s (§ Snapshot and Change Reconciliation). This does not depend on any GitHub account permission beyond what issue/PR ingestion already uses (Contents API, same token).
2. **What is the most precise version identity for a changed package?** — answered by trying progressively cheaper/more-available sources, in the order given in the task:

| Priority | Source | Availability | What it adds |
|---|---|---|---|
| 1 | GitHub dependency-review compare API (`GET /repos/{owner}/{repo}/dependency-graph/compare/{base}...{head}`) | Requires Dependency Graph enabled on the repo; commonly 403/404/422 on repos without it | Resolved version, `package_url` (purl), best-effort direct/transitive `relationship` |
| 2 | Dependabot PR metadata (title `Bump {package} from {old} to {new}`, or pip's `Update {package} requirement from {old} to {new} in {path}` — Dependabot's wording varies by ecosystem/updater, not just by package; both are recognized) | Available whenever the PR is Dependabot-authored — zero new API scope beyond existing issue/PR sync | Exact resolved from/to version, even when the manifest itself only expresses a range |
| 3 | Raw manifest-declared text captured by the snapshot diff itself | Always available | Whatever the manifest literally says (exact pin or bare constraint) |

Source 1 failing (any non-2xx) is treated as "unavailable for this PR," logged once, and never aborts the poll cycle — per the HLD non-goal, an unavailable GitHub dependency API must not fail unrelated polling. Source 2 is attempted only when source 1 didn't resolve this specific package (including the common case of a grouped Dependabot update, whose title doesn't enumerate every package in the group — those fall through to source 3 per package). Source 3 is the floor: it is what the snapshot diff already captured to *detect* the change in the first place, so there is always a value to record even when 1 and 2 both come up empty.

**Detection and identity are deliberately split.** Snapshot diffing decides *whether* a package's declared line changed — a plain text-equality check on the manifest content, correct regardless of whether either side is a resolved version or a bare range. Source 1/2 enrichment only decides what identity that already-detected change is *labeled* with. This is why source 1/2 being unavailable never causes a change to go undetected — it only lowers the precision of the version string recorded for it.

---

## Graph Model and Deterministic IDs

New node types, following the existing `idFrom` convention (`docs/llds/continuous-ci-ingestion.md § New Node Types`): `(lowercase-hyphenated-type, project_slug, natural-key...)`.

| Type | idFrom key | Key fields |
|---|---|---|
| `DependencyPackage` | `("dependency-package", project_slug, purl)` | `purl` (e.g. `pkg:pypi/bleak`, name-only, no version), `ecosystem` (e.g. `"pypi"`), `name` |
| `DependencyVersion` | `("dependency-version", project_slug, package_purl, version)` | `package_purl`, `version` (exact pin, resolved version, or raw declared constraint — see § Data Sources), `relationship` (`"direct"` \| `"transitive"` \| `"unknown"`) |
| `DependencyManifest` | `("dependency-manifest", project_slug, manifest_path)` | `manifest_path` (repo-relative), `ecosystem`, `format` (e.g. `"requirements-txt"`, `"pyproject-pep621"`) |
| `DependencySnapshot` | `("dependency-snapshot", project_slug, manifest_path, commit_sha)` | `manifest_path`, `commit_sha`, `captured_at` (commit/PR-merge timestamp — used to find "the prior snapshot," § below) |
| `DependencyChange` | `("dependency-change", project_slug, manifest_path, package_purl, commit_sha)` | `change_kind` (`"added"` \| `"removed"` \| `"changed"`), `version_source` (`"dependency_review"` \| `"dependabot_title"` \| `"manifest_diff"` — provenance of the version string, for auditability), `observed_at` |

New edges:

```
DependencyVersion -[:VERSION_OF]-> DependencyPackage        # written once, immutable
DependencySnapshot -[:CONTAINS]-> DependencyVersion          # one per package declared at that commit; written once, immutable
DependencySnapshot -[:FOR_COMMIT]-> Commit                   # gated on node_exists_by_parts; silently skipped if absent
DependencyChange -[:CHANGED_PACKAGE]-> DependencyPackage
DependencyChange -[:FROM_VERSION]-> DependencyVersion        # omitted when change_kind == "added"
DependencyChange -[:TO_VERSION]-> DependencyVersion          # omitted when change_kind == "removed"
DependencyChange -[:INTRODUCED_BY]-> Commit                  # gated; see § Reconciliation
DependencyChange -[:MERGED_VIA]-> Fix                        # gated; reuses the existing Fix node, no new PullRequest type
DependencyManifest -[:DECLARES]-> DependencyVersion           # current-state; reconciled to the latest snapshot's CONTAINS set
File -[:USES_DEPENDENCY]-> DependencyPackage                  # current-state; reconciled per File from code-map imports
```

`DependencyManifest -[:DECLARES]->` is a stable, always-current entry point ("what does this manifest declare *right now*") so a query doesn't need to know which `DependencySnapshot` is most recent — the same "reconcile the current-state view, keep history immutable underneath" split `WorkflowRun`'s `TESTED_COMMIT` reconciliation already establishes (`docs/llds/continuous-ci-ingestion.md § Targeted vs. Tested Commit`). Every time a new `DependencySnapshot` is written for a manifest, `DependencyManifest -[:DECLARES]->` is replaced (`replace_edges_by_parts`) to point at exactly that snapshot's `CONTAINS` targets. The snapshot itself, and every past `DependencyChange`, are never deleted or overwritten — additive history underneath a reconciled current-state pointer, per the task's "preserve historical snapshots and changes; reconcile only current-state relationships."

No dedicated "current snapshot" edge is needed to find *the prior snapshot* for diffing: `DependencySnapshot.captured_at` is a plain property, so "the snapshot immediately before this one" is `MATCH (s) WHERE s.node_type='DependencySnapshot' AND s.project_slug=$p AND s.manifest_path=$m AND s.captured_at < $captured_at RETURN s ORDER BY s.captured_at DESC, s.commit_sha DESC LIMIT 1` — the same property-filtered query style the DRE already uses throughout (`node_type` is a property, never a real Quine label, per `docs/llds/diagnostic-retrieval-engine.md § Graph-first`). The `commit_sha` tiebreaker exists only to make the query result deterministic when two snapshots share a `captured_at` (e.g. two merges within the same second); it does not claim to encode true git ancestry (§ Open Questions).

---

## Polling and Checkpoint Behavior

New `ProjectConfig` field: `last_dependency_sync: str | None` (`src/modok/cli/config.py`) — its own cursor, independent of `last_github_sync` and `last_workflow_sync`, persisted via the existing `_update_project_config_field` helper (`src/modok/ingestion/git_history.py`). That helper's block-boundary handling (stopping at any `[section]`, not just another `[[projects]]`) was already fixed live in this session's working tree before this LLD was drafted — a prerequisite this field's safe persistence depends on, not new work introduced here.

New step in `GitHubPollAdapter._poll_once` (`src/modok/webhook/adapters/github_poll.py`), added after the existing CI-ingestion step, with the same isolation discipline: its own `try`/`except`, unable to block issue/PR sync or CI ingestion, and vice versa.

1. Fetch merged PRs with `updated_at > last_dependency_sync` — the same paginated `GET /pulls?state=closed&sort=updated&direction=desc` endpoint `GithubIngester`'s own PR sync already calls (`docs/llds/github-ingestion.md § API Access Pattern`). This is a second, independent fetch, not a shared cache — kept separate so this step's own retry/failure timing never couples to issue/PR sync's cursor, matching the "own cursor" requirement.
2. Process the fetched PRs **oldest-`updated_at`-first**. For each: fetch its changed-files list (`GET /pulls/{number}/files`, paginated); intersect paths against the tracked-manifest table (§ below). No match → no-op for this PR (the common case).
3. For each touched manifest path: fetch full content at `merge_commit_sha` via the Contents API — never `head.sha` (the PR branch's own tip), which for a squash or rebase merge may not even exist in default-branch history, and for a regular merge can still reflect a stale, un-rebased branch state (this matches the existing `IMPLEMENTED_IN` precedent, `docs/llds/github-ingestion.md § Field mapping — Fix`, which keys on `merge_commit_sha` for exactly this reason) — then parse per-ecosystem (§ File Format Parsing), upsert the `DependencySnapshot` (idempotent on its key — reprocessing the same PR is a no-op), find the prior snapshot (if any) and diff (§ Snapshot and Change Reconciliation), apply version-fidelity enrichment (§ Data Sources) per changed package, write `DependencyChange` nodes/edges.
4. **Advance `last_dependency_sync` to this PR's own `updated_at` only immediately after this PR finishes** (success, or a clean no-match no-op) — not after the whole batch. A failure processing PR *N* leaves the cursor at PR *N-1*'s timestamp, so PR *N* (and anything newer, already fetched this cycle) is naturally refetched and retried on the next cycle, without a separate backlog structure.

This is a deliberately simpler cursor model than continuous CI ingestion's discovery/expansion split (`docs/llds/continuous-ci-ingestion.md § Poll Cycle Extension`). CI ingestion decoupled discovery from expansion because expanding a workflow run is slow, multi-stage, and artifact-heavy — a single bad run must not block newer ones from ever being *discovered*. Dependency processing per PR is a single bounded operation (one files-list fetch, one-or-few Contents fetches, one parse, one diff, one set of writes) — cheap enough that coupling "discovered" and "processed" into one per-PR cursor advance is simpler and still satisfies the same failure-isolation intent, without a second state machine to maintain.

### Reconciliation

Because `Commit` nodes arrive via the separate, local `ingest-git` path (not the GitHub poller) and `Fix` nodes for the *same* PR are written earlier in the *same* poll cycle by the existing issue/PR sync step, a `DependencyChange`'s `INTRODUCED_BY`/`MERGED_VIA` targets are not guaranteed to exist at the moment it is first written. Rather than leave a permanent gap (the same failure mode `docs/llds/continuous-ci-ingestion.md § Targeted vs. Tested Commit` found and fixed for `TARGETED_COMMIT`/`TESTED_COMMIT`), a reconciliation sweep runs once per poll cycle, per project, independent of the cursor above: for every `DependencyChange` missing `INTRODUCED_BY` or `MERGED_VIA` whose target now exists, write the edge (`write_edge_by_parts` — idempotent). Cheap, bounded by however many such gaps currently exist (typically zero).

### Tracked-Manifest Detection

A static filename table, mirroring the code map's own language-detection table (`docs/llds/code-map.md § Language Detection` — deterministic, no content sniffing):

| Filename pattern | Ecosystem | Parsed in v1? |
|---|---|---|
| `requirements*.txt` | pypi | Yes |
| `pyproject.toml` (`[project.dependencies]` only) | pypi | Yes |
| `Pipfile`, `poetry.lock`, `uv.lock` | pypi | Detected only — not parsed |
| `package.json`, `package-lock.json`, `yarn.lock` | npm | Detected only |
| `*.csproj`, `packages.config`, `Directory.Packages.props` | nuget | Detected only |
| `Gemfile`, `Gemfile.lock` | rubygems | Detected only |
| `go.mod`, `go.sum` | go | Detected only |
| `Cargo.toml`, `Cargo.lock` | cargo | Detected only |

"Detected only" means the PR is still recognized as manifest-touching (so it isn't silently dropped) and a `DependencyManifest` node is written, but no `DependencySnapshot`/`DependencyChange` content is parsed from it in v1 — mirroring the code map's own "Python only for symbol extraction; others deferred" precedent (`docs/llds/code-map.md § Decisions & Alternatives`).

Optional per-project config, `dependency_manifest_globs: list[str] | None` (mirrors `ci_artifact_pattern`'s opt-in shape): when set, narrows the static table's matches to paths also matching one of these globs — useful for a monorepo that wants to ignore vendored manifests elsewhere in the tree. `None` (default) tracks every statically-detected manifest path in the diff.

---

## File Format Parsing (v1: pypi only)

**`requirements*.txt`**: one dependency per line, PEP 508 simple form `name{comparator}version[,comparator version]*[; marker]`, optional extras (`name[extra1,extra2]`). Comment lines (`#`), blank lines, and directive lines (`-r other.txt`, `-e .`, `--index-url ...`) are skipped, not parsed as packages. A line that doesn't match the expected shape is skipped and logged (`parse_error`), not fatal to the rest of the file — same tolerance the code map's own `ast.parse()` failure handling uses (`docs/llds/code-map.md § Symbol Extraction`). The version field stored is the full comparator+version string as written (e.g. `>=0.22.0`), not just the number.

**`pyproject.toml`**: only the PEP 621 `[project.dependencies]` array is parsed, entry format identical to a requirements.txt line. `[tool.poetry.dependencies]` (Poetry's own table format, a different shape) is recognized as present but not parsed in v1 — an explicit, testable gap (§ Testable Non-Goals), not a silent one.

Package names are PEP 503–normalized (lowercased, runs of `-`/`_`/`.` collapsed to a single `-`) before building the purl, so `PySide6`, `pyside6`, and `PySide-6` all resolve to the same `DependencyPackage`.

---

## Snapshot and Change Reconciliation

Given a manifest's parsed `{package_name: declared_version_string}` set at `merge_commit_sha`:

1. Upsert `DependencySnapshot(manifest_path, commit_sha=merge_commit_sha, captured_at=<the merge commit's timestamp, or the PR's merged_at>)`.
2. For each `(package, version)` pair: upsert `DependencyPackage`/`DependencyVersion` and write `DependencySnapshot -[:CONTAINS]-> DependencyVersion`.
3. Find the prior snapshot for this manifest (§ Graph Model — `captured_at`-ordered query). **No prior snapshot → this is the manifest's first observed state; no `DependencyChange` records are written.** Every package in a repo's very first tracked snapshot is "first observed," not "changed" — writing a change record for each would flood the graph the moment MODOK starts tracking a repo that already has, say, forty existing dependencies, none of which actually changed. This is an explicit decision (§ Decisions & Alternatives), not an oversight.
4. With a prior snapshot: diff the two package sets by name. Added (in new, not old) → `DependencyChange(change_kind="added")`, `TO_VERSION` only. Removed (in old, not new) → `change_kind="removed"`, `FROM_VERSION` only. Present in both with a different raw string → `change_kind="changed"`, both `FROM_VERSION`/`TO_VERSION`. Unchanged → no record.
5. For each written `DependencyChange`, apply version-fidelity enrichment (§ Data Sources) to resolve the actual `DependencyVersion.version`/`relationship` values used in step 4's edges — enrichment can only refine which already-detected change gets which precision of version string, never invent an additional change.

Every write in this sequence is on a deterministic key (`upsert_node` / `write_edge_by_parts`, both idempotent) — reprocessing the same PR (retry, or a duplicate poll) produces the same `DependencySnapshot` and the same `DependencyChange`s, never duplicates.

---

## File-to-Dependency Usage Mapping

A separate, local concern from the GitHub-poller work above — imports are local source facts (already captured, unused, in `.modok/code-map.yml`'s `files[].imports`, per `docs/llds/code-map.md § Symbol Extraction`), not GitHub data. Keeping it out of the poller keeps the poller's API surface limited to package/version/manifest/change data.

New function, called once from `run_ingestion` (`src/modok/ingestion/pipeline.py`) after the existing per-doc write loop completes — a new call site, not new logic threaded through the existing, tested per-doc loop:

For each `.modok/code-map.yml` entry with `role: source` and `language: python`, **only if a `File` node already exists for that path** (`node_exists_by_parts(("file", project_slug, path))` — never invent a `File` node here; that remains `ingest_doc`'s job from the registry's declared `source_files`, per `docs/llds/ingestion-pipeline.md`):

1. For each import, take the top-level module name (`bleak.backends.scanner` → `bleak`).
2. Skip it if it's a standard-library module (`sys.stdlib_module_names`, deterministic, no LLM).
3. Resolve to a package name: an explicit override from `.modok/dependency-map.yml` (new, opt-in, checked-in, human-maintained — `import_overrides: {cv2: opencv-python}` shape) if present for that import name, else the import name itself (identity mapping — correct for the overwhelming majority of cases, including `bleak` → `bleak`).
4. PEP 503–normalize, build `pkg:pypi/{name}`. **Only if a `DependencyPackage` node with that purl already exists** (`node_exists_by_parts`) is it added to this file's target set — a file importing a package MODOK has never seen declared in any tracked manifest gets no edge yet; it self-heals the next time dependency ingestion (or `modok ingest`) runs after that package is discovered.
5. Reconcile (`replace_edges_by_parts`) the `File`'s `USES_DEPENDENCY` edges to exactly that target set, so a file that stops importing a package loses the edge on the next `modok ingest` run.

v1 scope: `File` only, not `TestFile` — matches the acceptance scenario's shape (a provisioning *source* file). Python only, matching § File Format Parsing.

---

## Existing Retrieval Integration

New evidence type in `retrieval/engine.py`: `dependency_change`, weight `5.0` — same tier as `element_anchor_match`/`function_anchor_match` (`docs/llds/diagnostic-retrieval-engine.md § Evidence Sources`), a level below primary-anchor evidence (`feature_primary_file`/`commit_message_match`, 9.0). **Applied only to files already present in the file-evidence map** — mirrors § Element Anchor Matching's "only files already in the evidence maps receive this evidence; element matching does not discover new files" rule exactly. `dependency_change` is corroborating, not added to `_NON_CORROBORATING_TYPES`.

This is what makes the two falsifiable properties true **by construction**, not by a recency-penalty formula bolted on afterward:

- **No ranking on recency alone**: the weight is a flat constant, never a function of how recently the change happened.
- **An unrelated but newer dependency update does not outrank it**: a `DependencyChange` only ever contributes evidence to a file reachable via that file's own `USES_DEPENDENCY` edge — a package an anchored file doesn't import never enters that file's evidence map, no matter how new the change is. An unrelated feature's dependency bump touches a different file's evidence map entirely, or none at all.

New traversal function `_traverse_files_to_recent_dependency_changes(file_paths, project_slug, client)`, called alongside the existing `_traverse_files_to_recent_commits` at the same point in `retrieve()` (`docs/llds/diagnostic-retrieval-engine.md § Recent commits`): for each anchored file path, `MATCH (f) WHERE id(f) = idFrom('file', ...) MATCH (f)-[:USES_DEPENDENCY]->(pkg) MATCH (pkg)<-[:CHANGED_PACKAGE]-(dc) MATCH (dc)-[:TO_VERSION]->(tv) OPTIONAL MATCH (dc)-[:FROM_VERSION]->(fv) OPTIONAL MATCH (dc)-[:INTRODUCED_BY]->(c) OPTIONAL MATCH (dc)-[:MERGED_VIA]->(fix) RETURN dc, pkg, fv, tv, c, fix`. Deduplicated by `DependencyChange` id. Deliberately **not** sorted/capped by recency the way `_traverse_files_to_recent_commits` caps at the 10 most recent (`docs/llds/diagnostic-retrieval-engine.md § Recent commits`) — capped instead by "belongs to an already-anchored file," which is inherently small.

New `DebugPacket` field (`retrieval/models.py`), sibling to `RecentCommit`:

```python
@dataclass
class RecentDependencyChange:
    package: str                    # purl, e.g. "pkg:pypi/bleak"
    from_version: str | None
    to_version: str
    manifest_path: str
    commit_sha: str | None
    fix_id: str | None              # existing Fix.fix_id, e.g. "gh-77"
    relationship: str                # "direct" | "transitive" | "unknown"
    files: list[str]                 # this retrieval's anchored files that import the package
    explanation: str                 # mechanical, e.g. "bleak 0.21 -> 0.22 (client/requirements.txt), used by client/stagehand_client/stagehand_ble.py"
```

`explanation` is a plain string template, not an LLM call — same "deliberately not natural language" discipline `quick_investigation_summary` already applies (`docs/llds/diagnostic-retrieval-engine.md § Quick Investigation Summary`). `recent_dependency_changes` is populated on both existing `DebugPacket` construction sites in `retrieve()` (the `"partial"` `on_progress` packet and the final return) — an in-segment cascade, since both already construct `DebugPacket` with `recent_commits` right next to where this field is added. Feeding `recent_dependency_changes` into `gateway.summarise_packet`'s prompt context is a Phase 6 detail; the acceptance scenario's "surfaces the bleak update as a relevant candidate" is satisfied at the `scored_candidates`/`recent_dependency_changes` level, not dependent on LLM summary wording.

---

## Failure Handling

| Condition | Behavior |
|---|---|
| Dependency-review API non-2xx (403/404/422/5xx) | Treated as unavailable for that PR; logged once at info level; falls through to Dependabot-title/manifest-text enrichment. Never aborts the cycle. |
| `GET /pulls/{number}/files` fails | This PR's dependency processing is skipped this cycle; cursor does not advance past it (§ Polling and Checkpoint Behavior) — retried next cycle. |
| Contents API 404 for a manifest at `merge_commit_sha` (file deleted/renamed in this PR) | If a prior snapshot exists, every package in it is recorded as `change_kind="removed"`; no new `DependencySnapshot` is written (there is no content to snapshot). |
| Malformed manifest line | That line is skipped and logged (`parse_error`); the rest of the file still parses — same tolerance as the code map's own per-file parse-error handling. |
| Rate limit (429) | Reuses `GithubIngester`'s existing `Retry-After`-aware handling (`docs/llds/github-ingestion.md § Rate Limiting`), extended to the new endpoints — same phrase and precedent continuous CI ingestion used for its own Actions-API extension. |
| `Commit`/`Fix` absent when a `DependencyChange` is first written | Edge silently skipped; picked up by the reconciliation sweep (§ Polling and Checkpoint Behavior) once the target exists. |
| `DependencyPackage` absent when `File` usage mapping runs | That file gets no `USES_DEPENDENCY` edge for that import yet; self-heals on a later `modok ingest` run after the package is discovered. Never invents the package node. |

---

## Testable Non-Goals

- No standing query, incident creation, or GitHub comment — this component only writes graph facts, per the HLD non-goal.
- No LLM-based import→package matching — a static stdlib skip-list, identity mapping, and the explicit `.modok/dependency-map.yml` override file only.
- Direct pushes to the default branch that change a manifest without a merged PR are not detected in v1 — there is no `Fix`/PR to anchor `MERGED_VIA` to, and this component's poll step is scoped to merged-PR discovery only. Explicitly deferred, not silently unhandled.
- Lockfile parsing, and true direct/transitive resolution beyond what the dependency-review API supplies, are deferred — v1 `manifest_diff`-sourced changes are always recorded `relationship="unknown"` unless source 1 supplied a value.
- Non-pypi manifest ecosystems are detected (so a touching PR isn't silently ignored) but not parsed for package/version content in v1.
- No CI pipeline changes, no new broker or polling service — one new step inside the existing `_poll_once` loop, one new call from the existing `run_ingestion` entry point.
- No new dependency dashboard. The only new UI-facing surface is the DRE's existing debug packet gaining `recent_dependency_changes` and `dependency_change` evidence items; any Demo UI rendering of that field is a Phase 6/implementation detail, not a new dashboard.
- `TestFile` dependency usage is not mapped in v1 — only `File` (source), matching the acceptance scenario's shape.
- Grouped Dependabot updates (one PR bumping several packages) are not enumerated from the PR title — each package within the group falls through to the raw manifest-text version (source 3), same as any non-Dependabot PR.
- **A version bump invisible in the manifest's own declared text is invisible to MODOK in v1.** Detection (§ Data Sources) is a text diff of the manifest's declared constraint, never the resolved/installed version. A floating range (`bleak>=0.22.0`) that resolves to a newer version on reinstall, with no lockfile and no edit to the manifest line itself, produces no `DependencyChange` — there is nothing to diff. This is a real, named limitation, not an oversight: Dependabot itself always rewrites the manifest line (even a floor bump), so Dependabot-driven updates remain detectable; a bump with no lockfile and no manifest edit at all is the specific case lockfile parsing (§ Open Questions, deferred) would close.

---

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|---|---|---|---|
| What actually detects a change | Deterministic snapshot-text diff, always-on | Relying solely on the dependency-review API | The API is commonly unavailable (permissions, feature flag); detection must not depend on it, or the acceptance scenario itself becomes non-deterministic |
| Version-fidelity source order | Dependency-review API → Dependabot PR title → raw manifest text | Manifest text only; LLM-assisted version resolution | Matches the task's explicit priority order; Dependabot reuse costs zero new API scope since MODOK already ingests and classifies Dependabot PRs |
| `MERGED_VIA` target | Existing `Fix` node | New `PullRequest` node type | "Use existing vocabulary where available" — `Fix` already carries `pr_url`, `kind`, and Dependabot detection for exactly this case |
| Cursor/failure-isolation model | Single coupled discovery+processing cursor, advanced per-PR after that PR completes | CI ingestion's decoupled discovery cursor + per-node expansion-state machine | Dependency processing per PR is one bounded operation, not multi-stage/artifact-heavy — the added complexity of a separate backlog structure isn't justified here |
| `INTRODUCED_BY`/`MERGED_VIA` reconciliation | Periodic sweep, once per cycle, for gaps whose target now exists | No sweep; accept permanent gaps if the target didn't exist yet | Same failure mode `TARGETED_COMMIT`/`TESTED_COMMIT` hit and fixed (`docs/llds/continuous-ci-ingestion.md`) — `Commit` nodes arrive via an independently-scheduled, local process |
| File→Package mapping location | Local `modok ingest` path, reading the code map | Fold into the GitHub poller | Imports are local source facts, not GitHub data; keeps the poller's API surface limited to package/version/manifest/change |
| First snapshot for a manifest | No `DependencyChange` records written | Record every existing dependency as an "added" change on first sight | Avoids flooding the graph with spurious change records the moment a repo starts being tracked; only real deltas between two observed snapshots count |
| `dependency_change` evidence weight | Flat 5.0, corroborating-only, target-file-gated | Recency-weighted (like `recent_commit`'s 1.5, decaying) | Flat + gated-to-already-anchored-files makes "no ranking on recency alone" and "unrelated newer update doesn't outrank" true by construction, not by tuning a decay curve |
| Import-name overrides | New checked-in `.modok/dependency-map.yml`, opt-in | LLM-assisted or PyPI-metadata-lookup resolution | Deterministic, offline, no LLM — matches every other "explicit mapping over inference" precedent in this codebase (registry `source_roots`, label-based `ticket_kind`) |
| v1 manifest parsing scope | pypi only (`requirements*.txt`, PEP 621 `pyproject.toml`) | All detected ecosystems | Mirrors the code map's own "Python only for symbol extraction; others deferred" precedent; matches the acceptance scenario |
| Diff baseline for a merged PR | The manifest's prior *observed* `DependencySnapshot` (by `captured_at`) | The PR's own `base_sha` content | Represents the default branch's true chronological dependency history regardless of PR review/merge timing — the same way `git log` on the default branch diffs each commit against its actual default-branch parent, not whatever stale branch a long-lived PR forked from. Diffing against a PR's own (possibly stale, unrebased) base could show a "change" that doesn't reflect the current default-branch state, or miss one that an intervening merge already introduced |

---

## Open Questions & Future Decisions

### Deferred

1. **Lockfile parsing** (`poetry.lock`, `package-lock.json`, `uv.lock`, ...) — would materially improve direct/transitive fidelity beyond what the dependency-review API supplies, but is a meaningfully different parser shape per ecosystem. Deferred until a concrete need for transitive-dependency evidence is demonstrated.
2. **Grouped Dependabot PR title parsing** — Dependabot's "group" update title format doesn't enumerate individual package versions; a future slice could parse the PR body's per-package table instead. Not needed for the single-package acceptance scenario.
3. **`.modok/dependency-map.yml` schema details beyond `import_overrides`** — whether it also wants a manifest allowlist/denylist independent of `dependency_manifest_globs`, or per-ecosystem sections, is a Phase 6 detail once the override file has a real second use case.
4. **Non-pypi parsing** (npm, nuget, cargo, go, rubygems) — detection-only in v1; parser implementations are additive, one ecosystem at a time, following the same `{package, version}` extraction shape already established for pypi.
5. **Direct pushes without a PR** — out of scope for v1 (§ Testable Non-Goals); revisit only if a project's workflow relies on unreviewed pushes to the default branch changing manifests.
6. **No backfill cap on first sync.** `last_dependency_sync` empty means "fetch all," same as `last_github_sync` today — an old project's first cycle walks its entire merged-PR history. Deliberately not given a dependency-specific cap (unlike `ingest-git`'s `--max-commits 500`): the expensive part (files-list + Contents fetches) only fires for PRs that already matched a tracked manifest path, which self-limits in practice, and issue/PR sync has the identical unbounded-first-sync characteristic today without a documented problem. If this ever needs bounding, it should be solved once, for both, not as a one-off here.
7. **Manifest renames are not special-cased.** GitHub's PR files API reports `status: "renamed"` with `previous_filename`, which this design does not currently consult. A renamed manifest's old path simply stops accumulating snapshots (still a valid historical record) and the new path starts a fresh "first snapshot" (no spurious change flood, per the first-snapshot rule above) — the only cost is one missed `DependencyChange` for the rename event itself. Low severity, self-contained; deferred rather than adding rename-migration logic for a rare event.

---

## References

- `docs/high-level-design.md § GitHub Dependency-Graph Ingestion` — why this exists, its non-goal boundary, and the falsifiable claim it must satisfy
- `docs/llds/github-ingestion.md § Field mapping — Fix, § API Access Pattern, § Rate Limiting` — `Fix`/Dependabot detection and the GitHub API patterns this component reuses
- `docs/llds/continuous-ci-ingestion.md § Poll Cycle Extension, § Targeted vs. Tested Commit` — the poll-cycle isolation and reconciliation-sweep precedent this component follows at a smaller scale
- `docs/llds/code-map.md § Symbol Extraction, § Language Detection` — the `imports` data this component is the first consumer of, and the static-table detection style it mirrors
- `docs/llds/ingestion-pipeline.md` — where `File` nodes are written from registry-declared `source_files`, which `USES_DEPENDENCY` edges are gated on
- `docs/llds/diagnostic-retrieval-engine.md § Evidence Sources, § Recent commits, § Element Anchor Matching` — the evidence-scoring and recent-change retrieval patterns this component extends
- `docs/llds/quine-client.md` — `upsert_node`/`write_edge_by_parts`/`replace_edges_by_parts`/`node_exists_by_parts` primitives reused for all new node/edge types
