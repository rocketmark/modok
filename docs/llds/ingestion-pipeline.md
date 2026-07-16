# Ingestion Pipeline

## Context and Design Philosophy

The ingestion pipeline is MODOK's write path for trusted knowledge. It reads design docs, testing docs, code maps, known issues, resolved tickets, and runbooks, then writes typed, validated nodes and edges into Quine.

The core discipline: **the parser is mechanical; the LLM is a proposer.** No LLM output is written to Quine without passing through a validation gate. The graph is only as trustworthy as the ingestion pipeline that feeds it.

Three principles govern every decision in this layer:

- **Convention + registries are truth for structure.** Doc type and feature ownership are inferred from path conventions and the arrow index. Registries supply module, source file, and test file lists. Frontmatter is an override escape hatch, not a requirement.
- **Fail loudly on invalid references.** A doc that resolves to a feature slug not in the feature registry is an error. The graph must not contain dangling references.
- **Idempotent by design.** Running ingestion twice on the same inputs produces the same graph. Re-ingesting after a doc or commit edit updates stale properties; it does not create duplicates.

## Ingestion Trigger Model

Ingestion runs are triggered by a git post-commit hook, installed per-project via `modok init`. The hook runs after any commit that touches paths registered in `~/.modok/config.toml` for the project (docs, registries, tickets).

The hook is opt-in — `modok init --project stagehand --repo ./` installs it. It does not run on every commit unconditionally; it checks whether any changed file matches the project's registered ingestion paths before invoking the pipeline. On a commit that touches only source code, the hook exits immediately.

`modok init` appends a clearly marked MODOK section to any existing post-commit hook rather than overwriting it. If a MODOK section already exists in the hook, it replaces only that section. It never errors out because another hook tool is present.

Manual invocation is always available:

```bash
modok ingest --project stagehand                    # docs and registries
modok ingest --project stagehand tickets/T-001.md    # a single customer ticket file
modok extract-code-map --project stagehand           # code map (source tree facts)
```

A CI step can be added later to keep the shared Mac mini's graph in sync with the remote repo.

## Commit SHA Tracking

The following node types carry commit SHA fields:

| Node | Field | Required? | Purpose |
|---|---|---|---|
| `Fix` | `commit_sha` | Required | The commit that introduced the fix |
| `ResolutionEvent` | `commit_sha` | Required | The commit at time of resolution |
| `KnownIssue` | `commit_sha` | Optional | The commit that introduced the issue, if known |

The ingestion pipeline populates `commit_sha` from `git log --format=%H -1 -- <file_path>` on the file being ingested, using the most recent commit that touched that file. For `Fix` and `ResolutionEvent`, the SHA is required in the source YAML; ingestion fails loudly if it is missing.

When ingestion is run manually with uncommitted changes in the working tree, the pipeline emits a visible warning: "working tree is dirty — SHA on ingested nodes reflects last commit, not current state." Ingestion completes normally; the SHA is diagnostic metadata, not a key.

## Source Formats

### Supported file types (v1)

```
.md   .mdx   .yaml   .yml
```

### Doc discovery — three-tier approach

Doc metadata is inferred from conventions and registries. Frontmatter is an override only; no doc requires it.

**Tier 1 — Arrow-index-driven (primary)**

Walk `docs/arrows/index.yaml`. For each arrow entry, discover the associated docs by following the registered paths:

| Arrow field | doc_type assigned |
|---|---|
| `arrow_doc` | `hld` |
| `lld` | `lld` |
| `specs` | `spec` |

Feature is the arrow's `id`. Modules, source_files, and test_files are looked up from `features.yml` and `modules.yml` — the registries are the source of truth. No frontmatter required for any of these.

Any of `arrow_doc`, `lld`, or `specs` may be a YAML list instead of a single string, when a feature genuinely spans more than one doc of that type (e.g. a client-side and a Pi-side LLD for one arrow). Each path in the list is ingested as its own Tier-1 doc record with the same `doc_type` and `feature`.

**Tier 2 — Path-based inference (secondary)**

After Tier 1, scan `docs/` for any `.md` file not already discovered. Infer `doc_type` from the containing directory and `feature` from the filename stem:

| Directory | Inferred doc_type | Feature inference |
|---|---|---|
| `docs/llds/` | `lld` | stem (`pi-agent.md` → `pi-agent`) |
| `docs/arrows/` | `hld` | stem |
| `docs/specs/` | `spec` | stem, stripping `-specs` suffix if present |
| `docs/` (root) | `hld` | stem |
| Other `docs/**` | attempt inference | stem |

After inference, look up the inferred feature slug in `features.yml`. If found: ingest with full registry-derived metadata. If not found: proceed to Tier 3.

**Tier 3 — Unregistered**

Docs that do not resolve to a known feature slug after Tier 1 and Tier 2 are assigned `doc_type: unregistered`. They are ingested as bare `Doc` nodes with no `Feature`, `Module`, or `File` edges. They are surfaced in the ingestion report as a discovery signal:

```
3 unregistered docs (no matching feature slug):
  docs/frontmatter.md
  docs/new-extraction-brainstorm.md
  docs/archived/hifi-brainstorm.md
```

Unregistered docs are not errors or warnings. They indicate docs that need an arrow, a manual frontmatter override, or deletion.

**Frontmatter as override**

Any inferred field can be overridden by an explicit frontmatter block. The override need only declare the fields being overridden:

```yaml
---
modok:
  feature: pi-agent          # overrides stem inference
  doc_type: adr              # overrides directory inference
---
```

Fields not present in frontmatter fall through to inference. A doc with no frontmatter at all is fully inference-driven.

### Inline MODOK blocks

Within doc body, structured facts are declared in fenced MODOK blocks:

```markdown
## Failure Modes

### Version mismatch

```modok
kind: failure_mode
id: shtp-version-mismatch
symptom: Agent sends v2 header; client rejects with "unsupported version"
affects:
  - feature:shtp-receiver
  - module:shtp
relevant_files:
  - agent/src/shtp.c
relevant_tests:
  - agent/tests/test_shtp.c
```
```

### Known issue and fix blocks

```markdown
## Known Issues

### Version mismatch corrupts calibration

```modok
kind: known_issue
id: ki-shtp-version-mismatch
summary: Client rejects v2 header due to version field mismatch
status: open
affects:
  - feature:shtp-receiver
error_signatures:
  - shtp-version-mismatch
fixes:
  - fix-shtp-version-offset
```
```

`error_signatures` and `fixes` are optional lists on a `known_issue` block. Each entry writes an edge — `error_signatures` writes `KnownIssue -[:HAS_ERROR]-> ErrorSignature`; `fixes` writes `KnownIssue -[:RESOLVED_BY]-> Fix`. Both follow the confidence-1.00, no-existence-check convention already used for `affects`: a `known_issue` block is source-of-truth, MODOK-block-declared content, so the edge target may be written as a shell node if the referenced `Fix`/`ErrorSignature` hasn't been ingested yet in this run (`quine-client.md § Edge-before-node writes are permitted`) — the target is expected to be promoted to a full node by its own ingestion (a `fix` block or a doc's frontmatter `error_signatures`), not invented here.

These two edges are what let a standing query (`docs/llds/standing-queries.md`) observe that a `KnownIssue` already has a documented fix for a known error — before this addition, `KnownIssue -[:HAS_ERROR]->` and `KnownIssue -[:RESOLVED_BY]->` were schema-documented and DRE-consumed (`diagnostic-retrieval-engine.md`, `modok diagnose`) but not written by any ingestion code path; they were only exercised by hand-built test fixtures.

### Ticket YAML

```yaml
ticket_id: gh-142
source_system: github
feature: shtp-receiver
symptoms:
  - pose-dropout
observed_errors:
  - shtp-version-mismatch
environment:
  platform: windows
status: unresolved
```

### Resolution YAML

```yaml
ticket_id: gh-142
source_system: github
feature: shtp-receiver
error_signature: shtp-version-mismatch
root_cause: Client was comparing version field at wrong byte offset
fix:
  kind: code_fix
  fix_id: fix-shtp-version-offset
  commit_sha: a3f9c12
  files_changed:
    - client/shtp_receiver.py
tests_added:
  - client/tests/test_shtp_receiver.py
commit_sha: a3f9c12
status: resolved
```

## Registries

All registries live in the project repo under `registries/` at the repo root. They are version-controlled alongside the docs they describe, travel with the code, and are reviewed in PRs. The `~/.modok/config.toml` points to the project repo root; MODOK finds registries at `{repo_root}/registries/`.

`~/.modok/` is for runtime state only (graph data, config, PID file) — never for source-of-truth metadata.

### Feature registry (`features.yml`)

```yaml
features:
  shtp-receiver:
    name: SHTP Receiver
    product_area: networking
    aliases:
      - shtp receiver
      - shtp v2
```

### Module registry (`modules.yml`)

```yaml
modules:
  shtp:
    name: SHTP
    source_roots:
      - agent/src
    test_roots:
      - agent/tests
```

### Error signature registry (`errors.yml`)

```yaml
errors:
  shtp-version-mismatch:
    text: "unsupported version"
    feature: shtp-receiver
    module: shtp
    tags:
      - protocol
      - versioning
```

### Doc type registry (`doc-types.yml`)

```yaml
doc_types:
  hld:
    required_fields:
      - feature
      - product_area
  lld:
    required_fields:
      - feature
      - modules
      - source_files
  testing:
    required_fields:
      - feature
      - test_files
  known-issue:
    required_fields:
      - feature
      - error_signatures
```

## Git History Ingestion

### Command interface

```bash
modok ingest-git --project <slug> [--repo <path>] [--full] [--since <date>] [--max-commits N]
```

- `--full` — import all history; no lookback limit. Use for initial bootstrap only.
- `--since <date>` — import commits after this date (ISO-8601). Overrides `--max-commits`.
- `--max-commits N` — import at most N commits. Default: 500.
- Default (no flags): import commits from the last 6 months, up to 500 commits.

The post-commit hook calls `modok ingest-git` automatically after each commit, adding exactly one commit node per trigger.

### What is imported

Only commits that touch **registered source files** — files that appear in any feature's `source_files` list in `features.yml`, or in any doc path registered in the arrow index — are imported. Commits touching only unregistered files are skipped.

This filter is applied using `git log --diff-filter=ACMR -- <registered_files>` to avoid importing infrastructure commits (dependency lock updates, CI config changes) that are irrelevant to the feature graph.

### Commit node schema

```
Commit
  id:           idFrom("Commit", projectSlug, sha)
  sha:          str               # full 40-char SHA
  timestamp:    datetime          # ISO-8601, author date
  author_name:  str
  author_email: str
  message:      str               # first line only, max 120 chars
  branch:       str | null        # branch name at time of ingest; null if detached
```

### Edge: TOUCHES

For each file changed in an imported commit (added, modified, renamed, deleted):

```
(:Commit)-[:TOUCHES {change_type: "M" | "A" | "D" | "R"}]->(:File)
```

`change_type` mirrors git's diff filter codes. Deleted files (`D`) create the edge even if the `File` node has no current on-disk presence — the commit is historical record.

### Incremental ingestion

The pipeline tracks the most recently ingested SHA per project in `~/.modok/config.toml` under `[projects.{slug}] last_git_sha`. On incremental runs:

1. Read `last_git_sha` from config.
2. Run `git log {last_git_sha}..HEAD -- <registered_files>`.
3. Import only new commits.
4. Update `last_git_sha` to `HEAD` SHA after a successful run.

On the first run (no `last_git_sha`), the lookback window applies.

### Enabling temporal queries

Commit nodes enable queries such as:

- "Which commits have touched both `tracker.c` and the pi-agent LLD in the last 90 days?"
- "What files changed in the same commit that last modified `recovery.c`?"
- "Show me all commits touching pi-agent source files since the last release tag."

These are Cypher traversals on the `TOUCHES` edges without any additional indexing.

---

## Parser Pipeline

For each ingested file the pipeline runs these stages in order. Any stage that fails halts ingestion for that file and emits a structured error — it does not silently continue.

```
1. Discover docs
   └── Tier 1: walk docs/arrows/index.yaml; collect registered LLD/spec/hld paths
   └── Tier 2: scan docs/ for .md files not already discovered; infer doc_type + feature from path
   └── Tier 3: docs with unresolved feature → doc_type: unregistered
   └── apply ignore rules (same as SI-DISC-002)

2. Resolve metadata
   └── for each doc, merge: inferred metadata ← frontmatter overrides
   └── look up modules/source_files/test_files from registries (for Tier 1 + 2 docs)

3. Validate references
   └── feature slug exists in feature registry (skip for unregistered docs)
   └── module slugs exist in module registry
   └── error signature slugs exist in error registry
   └── source_files and test_files exist on disk (missing → warning + confidence penalty)

4. Parse MODOK blocks
   └── extract structured facts from fenced modok blocks in body
   └── route each block fact at score=1.00 (verified; bypasses confidence model)

5. Extract headings
   └── parse H2/H3 headings as DocSection nodes
   └── write DESCRIBED_BY edges from Feature → DocSection
   └── unregistered docs: DocSection nodes written; no DESCRIBED_BY edge (no Feature)

6. Compute commit SHA
   └── git log --format=%H -1 -- <file_path>
   └── store on Doc node; null when file has no git history

7. LLM proposal pass (optional, --fix only)
   └── detect fields that could not be inferred and are not in frontmatter
   └── call LLM gateway propose_metadata(doc_path, inferred_metadata, missing_fields)
   └── call verifier: verify_proposal(proposal, missing_fields, inferred_metadata, registry)
   └── if any fields rejected and cegis_fix_enabled: one repair attempt (propose_metadata with repair_context)
   └── verify repaired proposal; accumulate valid_fields from both attempts
   └── default mode: write valid_fields to frontmatter override; warn per rejected field
   └── --strict mode: if any field rejected after repair, write nothing for this doc
   └── --emit-counterexamples: write YAML counterexample file to tests/fixtures/llm_gateway/
   └── --dry-run: print proposed patch, write nothing
   └── re-run stages 2–5 on updated metadata before writing to Quine

8. Write to Quine
   └── upsert nodes in dependency order
   └── write edges
   └── collect pending low-confidence facts in IngestionContext

9. End of run
   └── emit structured ingestion report (nodes written, pending count, duration, etc.)
   └── report unregistered doc count separately
   └── present pending low-confidence facts for interactive approval (--fix mode)
```

### Paths to ignore

```
.git/**         node_modules/**    bin/**         obj/**
dist/**         build/**           coverage/**    .vs/**
.env            *.key              *.pem          *.pfx
```

### Node write order

Nodes are written in dependency order to avoid dangling edge references (though Quine permits shell nodes, writing in order keeps the graph clean):

1. `Project`
2. `ProductArea`
3. `Feature`
4. `Module`
5. `File`
6. `Doc`
7. `Commit` (git history pass — `TOUCHES` edges written after all `File` nodes exist)
8. `ErrorSignature`
9. `FailureMode`
10. `Risk`
11. `KnownIssue`
12. `Fix`
13. `CustomerIssue` → `ResolutionEvent`

## Confidence Model

The confidence model applies only to facts extracted from prose and markdown structure — file path mentions in body text, error strings inferred from paragraphs, features inferred from headings. Facts declared explicitly in frontmatter or fenced MODOK blocks are always verified (1.00) by definition; they are hand-authored, schema-validated metadata and bypass scoring entirely.

The parser assigns a confidence band to each prose-extracted fact before writing it to Quine. Only facts at or above the `verified` threshold (0.90) are written automatically. Facts in `strong` (0.75–0.89) are written with a `confidence_low` / `confidence_high` property on the node. Facts below 0.75 are surfaced as warnings and require explicit approval before writing.

### Scoring

```python
def confidence_band(base, boosts=None, penalties=None, uncertainty=0.06):
    score = base + sum(boosts or []) - sum(penalties or [])
    score = max(0.0, min(1.0, score))
    return {
        "score": round(score, 3),
        "low": round(max(0.0, score - uncertainty), 3),
        "high": round(min(1.0, score + uncertainty), 3),
    }
```

### Base scores and adjustments

| Signal | Base / Adjustment |
|---|---|
| `file_path_regex` match | 0.85 |
| `markdown_link` to file | 0.88 |
| `quoted_error_string` | 0.82 |
| `symbol_pattern` | 0.65 |
| `heading_topic_match` | 0.70 |
| file exists on disk | +0.12 |
| appears in heading or MODOK block | +0.08 |
| appears multiple times | +0.05 |
| matches doc type context | +0.05 |
| multiple file matches | −0.20 |
| file not found on disk | −0.15 |
| ambiguous common word | −0.25 |
| generated or ignored path | −0.30 |

### Confidence thresholds

| Band | Range | Action |
|---|---|---|
| Verified | 0.90–1.00 | Write automatically |
| Strong | 0.75–0.89 | Write with confidence properties on node |
| Tentative | 0.55–0.74 | Surface as warning; require approval |
| Weak | 0.35–0.54 | Do not write; log for LLM proposal |
| Ignored | 0.00–0.34 | Discard |

## LLM Proposal Pass

The LLM gateway is invoked only when a doc is missing required metadata fields after the mechanical parse. It is never the primary parser.

### Caller responsibilities (ingestion pipeline)

1. Detect missing required fields by comparing present frontmatter keys against the doc type registry.
2. Call `gateway.propose_metadata(doc_path, frontmatter, missing_fields)`.
3. Call `verifier.verify_proposal(proposal, missing_fields, frontmatter, registry)` — a pure function in `modok/ingestion/verifier.py`.
4. If any fields are rejected and `cegis_fix_enabled = true`: build counterexamples from `rejected_fields`, call `gateway.propose_metadata(..., repair_context=counterexamples)` once.
5. Verify the repaired proposal. Accumulate `valid_fields` from both the initial and repair passes (a field that passed initially stays accepted even if the repair call omits it).
6. Apply the result per mode:
   - **Default (`--fix`)**: write `valid_fields` to doc frontmatter. Emit a structured warning per `rejected_field`. Ingestion continues.
   - **`--fix --strict`**: if any field remains rejected after repair, write nothing for this doc. Emit a structured error. Ingestion continues for other docs (exit `3` at end of run).
   - **`--fix --dry-run`**: print proposed patch and validation result. Write nothing. Always exits `0`.
   - **`--fix --emit-counterexamples`**: write a YAML counterexample file to `tests/fixtures/llm_gateway/` for each rejected field (both passes). File named `{doc_slug}_{iso_timestamp}.yaml`.
7. Re-run the mechanical parser on the updated frontmatter before writing to Quine.

Without `--fix` (default): no LLM calls are made. Missing fields are surfaced as warnings only. Source files are never mutated. CI runs are always safe without `--fix`.

In non-interactive mode (`sys.stdin.isatty()` returns `False`): all LLM proposal passes are suppressed, including the repair attempt. A single warning is emitted to stderr. See CLI-INGEST-004.

The LLM never writes to Quine directly. When `--fix` is used, it writes to the doc file; the mechanical parser then validates and writes to Quine.

## Output

Every ingestion run emits a structured report:

```
Ingestion complete
  Docs processed:  24
  Nodes written:   312
  Edges written:   487
  Warnings:        2
    - docs/lld/shtp.md: source_file 'agent/src/old_shtp.c' not found on disk
    - docs/lld/shtp.md: feature 'shtp-v1' not in feature registry
  Errors:          0
  LLM proposals:   0
  Pending items:   1
  Files ignored:   8
  Files skipped:   3
  Duration:        1.3s
```

Warnings do not halt ingestion. Errors do.

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|---|---|---|---|
| Doc discovery model | Three-tier: arrow index → path inference → unregistered | Frontmatter-required; LLM-parsed discovery | Arrow index is already maintained for LID workflow; path inference covers the 90% case; unregistered surfaces gaps without blocking ingestion |
| Frontmatter role | Override only; no fields required when convention applies | Always required; always optional | Required frontmatter was duplicating data already in the arrow index and registries; making it override-only eliminates that maintenance burden while keeping explicit control available |
| Unregistered doc type | Ingest as bare Doc node; surface in report | Skip silently; error; require frontmatter | Silent skip hides gaps; errors block ingestion for legitimate WIP docs; unregistered keeps graph complete and makes the gap visible |
| Git history scope | Only commits touching registered source/doc files | All commits; no filter | Unfiltered history includes infra noise (lock files, CI, tooling) that adds volume without adding diagnostic value; registered-file filter keeps commits relevant to the feature graph |
| Git history lookback | Default 6 months / 500 commits; `--full` for bootstrap | Fixed window; unlimited always | Unlimited on first run can be slow for old repos; default window captures recent history immediately; `--full` is a deliberate one-time operation |
| Git history update trigger | Post-commit hook (incremental, one commit per trigger) | Batch on each ingest run; manual only | Hook-driven incremental keeps history current automatically; batch re-import on every ingest run adds latency for no benefit after initial import |
| Commit filter — change types | A, C, M, R (added, copied, modified, renamed) | All changes including D | Deleted files leave no current artifact to traverse from; including D adds `TOUCHES` edges to non-existent File nodes; easier to omit D and let history emerge from surviving files |
| Trigger model | git post-commit hook, opt-in via `modok init` | Manual only; CI/CD only | Hook gives automatic local sync without CI infrastructure; opt-in avoids surprising repos that don't want it |
| Hook install on existing hook | Append MODOK section; replace if section exists | Overwrite; error if hook exists | Hooks are composable shell scripts; appending is the standard pattern and never loses existing tooling |
| Commit SHA source | `git log --format=%H -1 -- <file>` | Embedded in YAML; git hook env var `$GIT_COMMIT` | File-level SHA is always derivable without hook env; works for manual ingest runs too |
| Dirty working tree on manual ingest | Warn and complete | Block; silently use stale SHA | SHA is diagnostic metadata not a key; blocking is too disruptive for iterative doc editing |
| LLM write-back to doc | Opt-in via `--fix`; read-only by default | Always write-back; never write-back | Default read-only keeps CI safe; `--fix` enables the iterative proposal workflow for developers |
| LLM writes to doc, not Quine | LLM proposes → human approves → doc updated → parser writes Quine | LLM writes Quine directly; human reviews Quine nodes | Keeps Quine's write path mechanical; approved proposals become durable doc metadata that survives re-ingest |
| Verifier ownership | `modok/ingestion/verifier.py` | `modok/llm/verifier.py` | Verifier needs registry access (slug validation, enum values); gateway is stateless and registry-unaware; putting verifier in ingestion keeps the dependency flow clean |
| Partial field acceptance | Default: accept valid fields, warn per rejected | All-or-nothing only | All-or-nothing forces manual fix for any rejection; field-level acceptance lets easy fields land while hard ones re-surface naturally on next run; `--strict` preserves all-or-nothing for those who want it |
| `--strict` mode | Reject entire proposal if any field fails | Always all-or-nothing; always field-level | Gives callers explicit control; default is forgiving for interactive use; strict is appropriate for CI or audited ingestion |
| Counterexample emission | `--emit-counterexamples` flag writes YAML to `tests/fixtures/llm_gateway/` | Always emit; never emit; separate command | Optional flag keeps normal `--fix` output clean; emitting directly to the fixture path feeds the offline eval corpus without a separate step |
| Confidence model scope | Prose/structure extraction only; MODOK blocks always verified (1.00) | Apply scoring to all facts including explicit blocks | Explicit hand-authored metadata is always trusted; scoring prose inference is where the model adds value |
| Fail loudly on invalid references | Error + halt for that file | Warning + continue; auto-create missing registry entries | Dangling references in the graph corrupt retrieval; better to catch at ingest than debug at query time |
| Confidence threshold for auto-write | 0.90 (verified) | 0.75 (strong); 1.00 (only explicit) | 0.90 catches explicit metadata with minor uncertainty; avoids writing weak inferences as facts |
| Registry location | In-repo `registries/` directory, version-controlled | `~/.modok/projects/{slug}/registries/` (machine-local) | Registries are source-of-truth metadata; they belong with the code and docs they describe, not in machine-local state |
| Node write order | Dependency order (Project → Feature → ... → ResolutionEvent) | Unordered; edge-first | Keeps graph clean even though Quine permits shell nodes; easier to debug partial ingest runs |

## Open Questions & Future Decisions

### Resolved
1. ✅ Ingestion trigger — git post-commit hook, opt-in via `modok init`.
2. ✅ Hook install on existing hook — append MODOK section, replace if already present.
3. ✅ Commit SHA tracking — required on `Fix` and `ResolutionEvent`; optional on `KnownIssue`.
4. ✅ Dirty working tree — warn and complete; do not block.
5. ✅ LLM role — proposer only; write-back to doc is opt-in via `--fix`; never writes to Quine directly.
6. ✅ Confidence model scope — prose/structure extraction only; MODOK block content always verified (1.00).
7. ✅ Registry location — in-repo `registries/` directory, version-controlled.
8. ✅ Frontmatter role — override only; three-tier discovery makes frontmatter optional for convention-following docs.
9. ✅ Git history scope — registered source/doc files only; default 6 months / 500 commits; `--full` for bootstrap.

### Deferred
1. **Edge write order within a node** — SI-WRITE-001 mandates node write order; edge writes within a single node's context follow the order of the edge vocabulary table in the Quine client LLD. Not specified further; deterministic by construction from the model.
2. **Property caching risk** — SI-WRITE-003 requires full node re-upsert on re-ingest. The guard is that node objects must always be regenerated from the current doc, never cached from a prior parse. Enforce in code review, not in a spec.
3. **CI/CD sync to Mac mini** — when the Mac mini becomes the shared instance, a CI step will push ingestion runs to it. Mechanism (SSH + modok CLI, or a MODOK HTTP ingest endpoint) TBD.
2. **Incremental ingestion** — currently re-ingests all files on every run. For large doc trees, a file hash cache would skip unchanged files. Not needed at stagehand's doc volume.
3. **Multi-repo projects** — a project whose docs and code span multiple repos. Registry paths and file validation would need to be repo-relative. Deferred until a concrete case arises.
4. **LLM proposal review UX** — the CLI review prompt is one field at a time. For docs with many missing fields this could be slow. A batch review mode (show all proposals, approve/reject interactively) may be needed.
5. **Git commit filter — test_files** — SI-GIT-004's registered-file filter covers `source_files` and arrow doc paths but not `test_files`. Commits that add or modify tests are currently invisible to the feature graph. If queries like "what commits added tests for pi-agent?" are needed, add `test_files` paths to the filter scope.
6. **Unified `HAS_SECTION` traversal for registered docs** — unregistered docs use `Doc -[HAS_SECTION]-> DocSection`; registered docs use `Feature -[DESCRIBED_BY]-> DocSection`. These are two different traversal patterns to reach a `DocSection`. If a caller needs to find all sections for a given doc file regardless of registration status, add `File -[HAS_SECTION]-> DocSection` edges to registered doc ingestion to unify the pattern.

## References

- `docs/llds/quine-client.md` — write primitives used by this layer
- `docs/llds/llm-gateway.md` — LLM proposal interface; the ingestion pipeline must catch `LLMResponseError` and `LLMUnavailableError` from `propose_metadata` and emit a structured warning rather than halting the run (SI-LLM-003, LLM-META-004)
- `docs/llds/ingestion-pipeline-notes.md` — pre-draft notes
- `docs/testing-standard.md` — test level conventions
- `docs/modok-setup-brainstorm.md` §5 — original parser design
