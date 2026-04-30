# Static Ingestion Specs

Specs for `modok.ingestion` — the mechanical pipeline that reads docs, registries, tickets, and resolution records and writes typed, validated nodes and edges into Quine.

LLD: `docs/llds/static-ingestion.md`

---

## Test Level Convention

See `docs/testing-standard.md` for full definitions.

- **[U]** — Unit test with mocked dependencies.
- **[P]** — Property test (`hypothesis`). Implies [U].
- **[C]** — Contract test against live Quine instance. Implies [U].

---

## File Discovery

- [ ] **SI-DISC-001** [U]: The system shall discover all `.md`, `.mdx`, `.yaml`, and `.yml` files under the given ingestion path recursively.
- [ ] **SI-DISC-002** [P]: The system shall never ingest files matching any ignore pattern (`.git/**`, `node_modules/**`, `bin/**`, `obj/**`, `dist/**`, `build/**`, `coverage/**`, `.vs/**`, `.env`, `*.key`, `*.pem`, `*.pfx`).
- [ ] **SI-DISC-003** [U]: When a discovered file has no `modok:` frontmatter block, the system shall skip it without error and include it in a skipped-file count in the ingestion report.

---

## Frontmatter Parsing

- [ ] **SI-FMTR-001** [U]: The system shall parse the `modok:` YAML frontmatter block from each discovered file and validate that all required fields for the declared `doc_type` are present and well-formed. This stage validates schema structure only — it does not validate that slugs exist in registries. Registry reference validation is a separate subsequent stage (SI-REF-001 through SI-REF-005).
- [ ] **SI-FMTR-002** [U]: If a required frontmatter field is missing and `--fix` is not specified, the system shall emit a structured warning and skip writing that doc's nodes to Quine.
- [ ] **SI-FMTR-003** [U]: If a required frontmatter field is missing and `--fix` is specified, the system shall invoke the LLM gateway for a proposal, present it to the user for approval, write approved values back to the doc file, and re-run the mechanical parser on the updated file.
- [ ] **SI-FMTR-004** [U]: The system shall never write LLM proposals directly to Quine; proposals must be written to the doc file first and then pass through the mechanical parser.

---

## Reference Validation

- [ ] **SI-REF-001** [U]: When a frontmatter `feature` slug is not present in the feature registry, the system shall emit a structured error and halt ingestion for that file.
- [ ] **SI-REF-002** [U]: When a frontmatter `module` slug is not present in the module registry, the system shall emit a structured error and halt ingestion for that file.
- [ ] **SI-REF-003** [U]: When a frontmatter `error_signatures` entry is not present in the error registry, the system shall emit a structured error and halt ingestion for that file.
- [ ] **SI-REF-004** [U]: When a `source_files` or `test_files` path does not exist on disk, the system shall emit a structured warning (not an error) and apply a confidence penalty of −0.15 to any prose-extracted facts derived from that reference. Facts declared in frontmatter or MODOK blocks are not subject to this penalty — their confidence remains 1.00 regardless of file existence warnings.
- [ ] **SI-REF-005** [P]: The system shall never write a node or edge that references a slug not present in the relevant registry at the time of ingestion. When an invalid slug is detected, both the node carrying the reference and any edges that would connect to it via that slug are suppressed.

---

## MODOK Block Parsing

- [ ] **SI-BLOCK-001** [U]: The system shall parse fenced `modok` blocks in doc bodies and extract structured facts (failure modes, risks, decisions) as typed nodes.
- [ ] **SI-BLOCK-002** [U]: Facts declared in frontmatter or fenced MODOK blocks shall be assigned a confidence score of 1.00 (verified) and bypass the prose confidence scoring model entirely.
- [ ] **SI-BLOCK-003** [U]: When a fenced MODOK block contains an unrecognised `kind` value, the system shall emit a structured warning and skip that block without halting ingestion of the rest of the file.

---

## Heading and Line Range Extraction

- [ ] **SI-HEAD-001** [U]: The system shall extract H2 and H3 headings from each doc body and create a `DocSection` node for each, carrying the heading text, heading slug, line start, line end, and doc type.
- [ ] **SI-HEAD-002** [U]: The system shall write a `DESCRIBED_BY` edge from each `Feature` node referenced in the frontmatter to each `DocSection` node extracted from that doc.

---

## Commit SHA

- [ ] **SI-SHA-001** [U]: The system shall populate `commit_sha` on `Doc` and `DocSection` nodes by running `git log --format=%H -1 -- <file_path>` on the source file at ingest time. Git-log derivation applies to doc-type nodes only — not to `Fix` or `ResolutionEvent` nodes.
- [ ] **SI-SHA-002** [U]: When ingesting a `Fix` or `ResolutionEvent` YAML file that does not contain a `commit_sha` field, the system shall emit a structured error and halt ingestion for that file. The system shall not attempt to derive a SHA from git log for these node types — the SHA must be explicitly declared in the source YAML.
- [ ] **SI-SHA-003** [U]: When the working tree is dirty at the time of manual ingestion, the system shall emit a visible warning stating that commit SHAs reflect the last commit rather than the current working tree state, and shall complete ingestion normally.

---

## Confidence Model

- [ ] **SI-CONF-001** [P]: The system shall assign confidence bands only to facts extracted from prose and markdown structure; facts from frontmatter and MODOK blocks shall always receive a score of 1.00.
- [ ] **SI-CONF-002** [U]: The system shall automatically write prose-extracted facts with a computed confidence score of 0.90 or above to Quine without requiring approval. Frontmatter and MODOK block facts bypass this threshold entirely per SI-CONF-001.
- [ ] **SI-CONF-003** [U]: The system shall write facts with a confidence score between 0.75 and 0.89 to Quine with `confidence_low` and `confidence_high` properties on the node.
- [ ] **SI-CONF-004** [U]: The system shall not write prose-extracted facts with a confidence score below 0.75 to Quine without explicit user approval. At the end of each ingestion run, all pending low-confidence facts are batched and presented to the user for approval or rejection in a single interactive pass. They are not counted as warnings or errors in the ingestion report; they are counted separately as pending items.
- [ ] **SI-CONF-005** [P]: The system shall never produce a confidence score outside the range [0.0, 1.0].
- [ ] **SI-CONF-006** [U]: The system shall include a pending items count in the ingestion report when one or more prose-extracted facts have a confidence score below 0.75 and have not yet been approved or rejected.

---

## Node Write Order and Idempotency

- [ ] **SI-WRITE-001** [U, C]: The system shall write nodes to Quine in dependency order: Project → ProductArea → Feature → Module → File → Doc → DocSection → ErrorSignature → FailureMode → Risk → KnownIssue → Fix → CustomerIssue → ResolutionEvent.
- [ ] **SI-WRITE-002** [P, C]: Running ingestion twice on the same inputs shall produce the same graph state — no duplicate nodes, no duplicate edges, no orphaned nodes from the second run.
- [ ] **SI-WRITE-003** [U]: When a doc is updated and re-ingested, the system shall re-upsert the full node, replacing all properties with current values from the updated doc. Partial property updates are not permitted; the node must reflect exactly what the current doc declares.

---

## Registry Location

- [ ] **SI-REG-001** [U]: The system shall load feature, module, error, and doc type registries from `{repo_root}/registries/` in the project repo, not from `~/.modok/`.
- [ ] **SI-REG-002** [U]: When a registry file is missing from `{repo_root}/registries/`, the system shall emit a structured error and halt ingestion for the affected project.

---

## Ingestion Trigger — git hook

- [ ] **SI-HOOK-001** [U]: When `modok init --project {slug} --repo {path}` is run, the system shall install a post-commit hook in the project repo that runs ingestion after any commit touching registered ingestion paths.
- [ ] **SI-HOOK-002** [U]: The post-commit hook shall exit immediately without running ingestion when no changed file in the commit matches the project's registered ingestion paths.
- [ ] **SI-HOOK-003** [U]: When a post-commit hook already exists in the target repo, `modok init` shall append a clearly marked MODOK section rather than overwriting the existing hook.
- [ ] **SI-HOOK-004** [U]: When a MODOK section already exists in the post-commit hook, `modok init` shall replace only that section, leaving all other hook content unchanged.

---

## LLM Proposal Pass

- [ ] **SI-LLM-001** [U]: When `--fix` is specified and a doc is missing required metadata fields after the mechanical parse completes, the system shall invoke the LLM gateway for proposals. Without `--fix`, the system shall not invoke the LLM gateway at all — missing fields are reported as warnings only.
- [ ] **SI-LLM-002** [U]: When `--fix` is not specified, the system shall not invoke the LLM gateway and shall not modify any source file; missing required fields are emitted as structured warnings.
- [ ] **SI-LLM-003** [U]: When `--fix` is specified, the system shall write approved LLM proposals to the doc's frontmatter and re-run the mechanical parser on the updated file before writing to Quine.

---

## Ingestion Report

- [ ] **SI-RPT-001** [U]: The system shall emit a structured ingestion report after every run containing: docs processed, nodes written, edges written, warnings count, errors count, LLM proposals count, duration, files ignored (matched ignore patterns — SI-DISC-002), files skipped (present but no `modok:` frontmatter — SI-DISC-003), commits processed, and file changes written. Ignored and skipped are separate counts. Commits processed and file changes written are 0 when diff ingestion is not active.
- [ ] **SI-RPT-002** [U]: Warnings shall not halt ingestion; errors shall halt ingestion for the affected file and allow ingestion of remaining files to continue.

---

## Commit Diff Ingestion

- [ ] **SI-DIFF-001** [U]: When the post-commit hook fires and `source_paths` is configured for the project, the system shall parse the triggering commit's metadata (SHA, author, ISO timestamp, message first line) and upsert a `CommitEvent` node.
- [ ] **SI-DIFF-002** [U]: When `source_paths` is not configured for a project, or is configured as an empty list, the system shall skip commit diff ingestion entirely and emit no warnings.
- [ ] **SI-DIFF-003** [U]: For each file in the commit's diff that matches a configured `source_path`, the system shall upsert a `FileChange` node containing the repo-relative path, lines added, lines removed, and hunk headers (`@@ -a,b +c,d @@` lines only — no diff body text).
- [ ] **SI-DIFF-004** [U]: The system shall write a `File -[:CHANGED_IN]-> FileChange -[:IN_COMMIT]-> CommitEvent` edge chain for each file in the diff that matches a `source_path`.
- [ ] **SI-DIFF-005** [U]: For each `FileChange`, the system shall write a `CommitEvent -[:TOUCHES_FEATURE]-> Feature` edge for every Feature whose `source_files` or whose Module's `source_roots` contain the changed file's repo path. Each unique (CommitEvent, Feature) pair produces at most one `TOUCHES_FEATURE` edge.
- [ ] **SI-DIFF-006** [U]: Files in the commit diff that do not match any configured `source_path` shall be silently ignored by the diff ingestion stage; they are not reported as warnings or errors.
- [ ] **SI-DIFF-007** [P]: `CommitEvent` node ID shall be deterministic: `idFrom("CommitEvent", project_slug, commit_sha)`. `FileChange` node ID shall be deterministic: `idFrom("FileChange", project_slug, commit_sha, repo_path)`. Re-ingesting the same commit shall produce the same node IDs and upsert (not duplicate) existing nodes.
- [ ] **SI-DIFF-008** [U]: The system shall not store raw diff body text in any node or edge. Hunk headers only.
