# Ingestion Pipeline

## Context and Design Philosophy

The ingestion pipeline is MODOK's write path for trusted knowledge. It reads design docs, testing docs, code maps, known issues, resolved tickets, and runbooks, then writes typed, validated nodes and edges into Quine.

The core discipline: **the parser is mechanical; the LLM is a proposer.** No LLM output is written to Quine without passing through a validation gate. The graph is only as trustworthy as the ingestion pipeline that feeds it.

Three principles govern every decision in this layer:

- **Explicit metadata is truth.** Facts come from frontmatter, MODOK blocks, and registry entries — not from prose inference.
- **Fail loudly on invalid references.** A doc that references a feature slug that doesn't exist in the feature registry is an error, not a warning. The graph must not contain dangling references.
- **Idempotent by design.** Running ingestion twice on the same inputs produces the same graph. Re-ingesting after a doc edit updates stale properties; it does not create duplicates.

## Ingestion Trigger Model

Ingestion runs are triggered by a git post-commit hook, installed per-project via `modok init`. The hook runs after any commit that touches paths registered in `~/.modok/config.toml` for the project (docs, registries, tickets).

The hook is opt-in — `modok init --project stagehand --repo ./` installs it. It does not run on every commit unconditionally; it checks whether any changed file matches the project's registered ingestion paths before invoking the pipeline. On a commit that touches only source code, the hook exits immediately.

`modok init` appends a clearly marked MODOK section to any existing post-commit hook rather than overwriting it. If a MODOK section already exists in the hook, it replaces only that section. It never errors out because another hook tool is present.

Manual invocation is always available:

```bash
modok ingest-docs --project stagehand ./docs
modok ingest-tickets --project stagehand ./tickets
modok ingest-code-map --project stagehand ./src ./tests
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

### Doc frontmatter

Every ingested doc must carry a `modok:` block in its YAML frontmatter:

```yaml
---
modok:
  doc_type: lld                    # hld | lld | testing | runbook | known-issue | release-notes
  project: stagehand
  feature: shtp-receiver
  product_area: networking
  modules:
    - shtp
  source_files:
    - agent/src/shtp.c
    - agent/src/shtp.h
  test_files:
    - agent/tests/test_shtp.c
  error_signatures:
    - shtp-version-mismatch
  tags:
    - udp
    - protocol
---
```

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

## Parser Pipeline

For each ingested file the pipeline runs these stages in order. Any stage that fails halts ingestion for that file and emits a structured error — it does not silently continue.

```
1. Discover files
   └── walk paths, apply ignore rules

2. Parse frontmatter
   └── extract modok: block, validate against doc type registry

3. Validate references
   └── feature slug exists in feature registry
   └── module slugs exist in module registry
   └── error signature slugs exist in error registry
   └── source_files and test_files exist on disk

4. Parse MODOK blocks
   └── extract structured facts from fenced modok blocks in body

5. Compute commit SHA
   └── git log --format=%H -1 -- <file_path>

7. LLM proposal pass (optional)
   └── if required metadata is missing, call LLM gateway for suggestions
   └── surface proposals for review; do not write to Quine until approved

8. Write to Quine
   └── upsert nodes in dependency order
   └── write edges
   └── emit structured success/warning report
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
7. `ErrorSignature`
8. `FailureMode`
9. `Risk`
10. `KnownIssue`
11. `Fix`
12. `CustomerIssue` → `ResolutionEvent`

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

The proposal pass:
1. Sends the doc's frontmatter and a 500-token summary of the body to the LLM gateway.
2. Asks the LLM to suggest values for the missing fields.
3. Returns proposals to the CLI as a structured review prompt — one field at a time, with confidence and evidence.
4. With `--fix`: writes approved proposals back to the doc as explicit frontmatter, then re-runs the mechanical parser on the updated doc.
5. Without `--fix` (default): proposals are printed to stdout only. Source files are never mutated. CI runs are always safe to run without `--fix`.

The LLM never writes to Quine directly. When `--fix` is used, it writes to the doc file; the mechanical parser then validates and writes to Quine.

## Output

Every ingestion run emits a structured report:

```
Ingestion complete: stagehand
  Docs processed:     24
  Nodes written:      312
  Edges written:      487
  Warnings:           2
    - docs/lld/shtp.md: source_file 'agent/src/old_shtp.c' not found on disk (confidence: 0.71)
    - docs/lld/shtp.md: feature 'shtp-v1' not in feature registry
  Errors:             0
  LLM proposals:      0
  Duration:           1.3s
```

Warnings do not halt ingestion. Errors do.

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|---|---|---|---|
| Trigger model | git post-commit hook, opt-in via `modok init` | Manual only; CI/CD only | Hook gives automatic local sync without CI infrastructure; opt-in avoids surprising repos that don't want it |
| Hook install on existing hook | Append MODOK section; replace if section exists | Overwrite; error if hook exists | Hooks are composable shell scripts; appending is the standard pattern and never loses existing tooling |
| Commit SHA source | `git log --format=%H -1 -- <file>` | Embedded in YAML; git hook env var `$GIT_COMMIT` | File-level SHA is always derivable without hook env; works for manual ingest runs too |
| Dirty working tree on manual ingest | Warn and complete | Block; silently use stale SHA | SHA is diagnostic metadata not a key; blocking is too disruptive for iterative doc editing |
| LLM write-back to doc | Opt-in via `--fix`; read-only by default | Always write-back; never write-back | Default read-only keeps CI safe; `--fix` enables the iterative proposal workflow for developers |
| LLM writes to doc, not Quine | LLM proposes → human approves → doc updated → parser writes Quine | LLM writes Quine directly; human reviews Quine nodes | Keeps Quine's write path mechanical; approved proposals become durable doc metadata that survives re-ingest |
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

### Deferred
1. **Edge write order within a node** — SI-WRITE-001 mandates node write order; edge writes within a single node's context follow the order of the edge vocabulary table in the Quine client LLD. Not specified further; deterministic by construction from the model.
2. **Property caching risk** — SI-WRITE-003 requires full node re-upsert on re-ingest. The guard is that node objects must always be regenerated from the current doc, never cached from a prior parse. Enforce in code review, not in a spec.
3. **CI/CD sync to Mac mini** — when the Mac mini becomes the shared instance, a CI step will push ingestion runs to it. Mechanism (SSH + modok CLI, or a MODOK HTTP ingest endpoint) TBD.
2. **Incremental ingestion** — currently re-ingests all files on every run. For large doc trees, a file hash cache would skip unchanged files. Not needed at stagehand's doc volume.
3. **Multi-repo projects** — a project whose docs and code span multiple repos. Registry paths and file validation would need to be repo-relative. Deferred until a concrete case arises.
4. **LLM proposal review UX** — the CLI review prompt is one field at a time. For docs with many missing fields this could be slow. A batch review mode (show all proposals, approve/reject interactively) may be needed.

## References

- `docs/llds/quine-client.md` — write primitives used by this layer
- `docs/llds/llm-gateway.md` — LLM proposal interface; the ingestion pipeline must catch `LLMResponseError` and `LLMUnavailableError` from `propose_metadata` and emit a structured warning rather than halting the run (SI-LLM-003, LLM-META-004)
- `docs/llds/ingestion-pipeline-notes.md` — pre-draft notes
- `docs/testing-standard.md` — test level conventions
- `docs/modok-setup-brainstorm.md` §5 — original parser design
