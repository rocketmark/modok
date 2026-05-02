# Ingestion Pipeline Specs

Specs for `modok.ingestion` — the mechanical pipeline that reads docs, registries, tickets, and resolution records and writes typed, validated nodes and edges into Quine.

LLD: `docs/llds/ingestion-pipeline.md`

---

## Test Level Convention

See `docs/testing-standard.md` for full definitions.

- **[U]** — Unit test with mocked dependencies.
- **[P]** — Property test (`hypothesis`). Implies [U].
- **[C]** — Contract test against live Quine instance. Implies [U].

---

## File Discovery

- [x] **SI-DISC-001** [U]: The system shall discover all `.md`, `.mdx`, `.yaml`, and `.yml` files under the given ingestion path recursively.
- [x] **SI-DISC-002** [P]: The system shall never ingest files matching any ignore pattern (`.git/**`, `node_modules/**`, `bin/**`, `obj/**`, `dist/**`, `build/**`, `coverage/**`, `.vs/**`, `.env`, `*.key`, `*.pem`, `*.pfx`).
- [x] **SI-DISC-003** [U]: When a discovered file has no `modok:` frontmatter block, the system shall skip it without error and include it in a skipped-file count in the ingestion report.

---

## Frontmatter Parsing

- [x] **SI-FMTR-001** [U]: The system shall parse the `modok:` YAML frontmatter block from each discovered file and validate that all required fields for the declared `doc_type` are present and well-formed. This stage validates schema structure only — it does not validate that slugs exist in registries. Registry reference validation is a separate subsequent stage (SI-REF-001 through SI-REF-005).
- [x] **SI-FMTR-002** [U]: If a required frontmatter field is missing and `--fix` is not specified, the system shall emit a structured warning and skip writing that doc's nodes to Quine.
- [x] **SI-FMTR-003** [U]: If a required frontmatter field is missing and `--fix` is specified, the system shall invoke the LLM gateway for a proposal, run the verifier, apply validated fields to the doc file, and re-run the mechanical parser on the updated file before writing to Quine.
- [x] **SI-FMTR-004** [U]: The system shall never write LLM proposals directly to Quine; proposals must be written to the doc file first and then pass through the mechanical parser.

---

## Reference Validation

- [x] **SI-REF-001** [U]: When a frontmatter `feature` slug is not present in the feature registry, the system shall emit a structured error and halt ingestion for that file.
- [x] **SI-REF-002** [U]: When a frontmatter `module` slug is not present in the module registry, the system shall emit a structured error and halt ingestion for that file.
- [x] **SI-REF-003** [U]: When a frontmatter `error_signatures` entry is not present in the error registry, the system shall emit a structured error and halt ingestion for that file.
- [x] **SI-REF-004** [U]: When a `source_files` or `test_files` path does not exist on disk, the system shall emit a structured warning (not an error) and apply a confidence penalty of −0.15 to any prose-extracted facts derived from that reference. Facts declared in frontmatter or MODOK blocks are not subject to this penalty — their confidence remains 1.00 regardless of file existence warnings.
- [x] **SI-REF-005** [P]: The system shall never write a node or edge that references a slug not present in the relevant registry at the time of ingestion. When an invalid slug is detected, both the node carrying the reference and any edges that would connect to it via that slug are suppressed.

---

## MODOK Block Parsing

- [x] **SI-BLOCK-001** [U]: The system shall parse fenced `modok` blocks in doc bodies and extract structured facts (failure modes, risks, decisions) as typed nodes.
- [x] **SI-BLOCK-002** [U]: Facts declared in frontmatter or fenced MODOK blocks shall be assigned a confidence score of 1.00 (verified) and bypass the prose confidence scoring model entirely.
- [x] **SI-BLOCK-003** [U]: When a fenced MODOK block contains an unrecognised `kind` value, the system shall emit a structured warning and skip that block without halting ingestion of the rest of the file.

---

## Doc Section Extraction

- [x] **SI-HEAD-001** [U]: The system shall extract H2 and H3 headings from each ingested doc body and represent each as a `DocSection` node carrying the heading text, a slugified identifier, and the line range (line_start, line_end) within the doc. H1 headings shall not be extracted as `DocSection` nodes.
- [x] **SI-HEAD-002** [U]: For each `DocSection` extracted from a doc, the system shall write a `DESCRIBED_BY` edge from the doc's associated `Feature` node to the `DocSection` node.

---

## Commit SHA

- [x] **SI-SHA-001** [U]: The system shall call `git log --format=%H -1 -- <file_path>` to obtain the most recent commit SHA for each `Doc` and `DocSection` node, and store it on the node. When the file has no git history (e.g. untracked), the SHA field shall be `null`. The system shall not call `get_commit_sha` for `Fix` or `ResolutionEvent` nodes — their SHA must be declared explicitly in the source YAML.
- [x] **SI-SHA-002** [U]: When ingesting a `Fix` or `ResolutionEvent` YAML file that does not contain a `commit_sha` field, the system shall emit a structured error and halt ingestion for that file. The system shall not attempt to derive a SHA from git log for these node types — the SHA must be explicitly declared in the source YAML.
- [x] **SI-SHA-003** [U]: When the working tree is dirty at the time of manual ingestion, the system shall emit a visible warning stating that commit SHAs reflect the last commit rather than the current working tree state, and shall complete ingestion normally.

---

## Confidence Model

- [x] **SI-CONF-001** [P]: The system shall assign confidence bands only to facts extracted from prose and markdown structure; facts from frontmatter and MODOK blocks shall always receive a score of 1.00.
- [x] **SI-CONF-002** [U]: The system shall automatically write prose-extracted facts with a computed confidence score of 0.90 or above to Quine without requiring approval. Frontmatter and MODOK block facts bypass this threshold entirely per SI-CONF-001.
- [x] **SI-CONF-003** [U]: The system shall write facts with a confidence score between 0.75 and 0.89 to Quine with `confidence_low` and `confidence_high` properties on the node.
- [x] **SI-CONF-004** [U]: The system shall not write prose-extracted facts with a confidence score below 0.75 to Quine without explicit user approval. At the end of each ingestion run, all pending low-confidence facts are batched and presented to the user for approval or rejection in a single interactive pass. They are not counted as warnings or errors in the ingestion report; they are counted separately as pending items.
- [x] **SI-CONF-005** [P]: The system shall never produce a confidence score outside the range [0.0, 1.0].
- [x] **SI-CONF-006** [U]: The system shall include a pending items count in the ingestion report when one or more prose-extracted facts have a confidence score below 0.75 and have not yet been approved or rejected.

---

## Node Write Order and Idempotency

- [x] **SI-WRITE-001** [U, C]: The system shall write nodes to Quine in dependency order: Project → ProductArea → Feature → Module → File → Doc → ErrorSignature → FailureMode → Risk → KnownIssue → Fix → CustomerIssue → ResolutionEvent.
- [x] **SI-WRITE-002** [P, C]: Running ingestion twice on the same inputs shall produce the same graph state — no duplicate nodes, no duplicate edges, no orphaned nodes from the second run.
- [x] **SI-WRITE-003** [U]: When a doc is updated and re-ingested, the system shall re-upsert the full node, replacing all properties with current values from the updated doc. Partial property updates are not permitted; the node must reflect exactly what the current doc declares.

---

## Registry Location

- [x] **SI-REG-001** [U]: The system shall load feature, module, error, and doc type registries from `{repo_root}/registries/` in the project repo, not from `~/.modok/`.
- [x] **SI-REG-002** [U]: When a registry file is missing from `{repo_root}/registries/`, the system shall emit a structured error and halt ingestion for the affected project.

---

## Ingestion Trigger — git hook

- [x] **SI-HOOK-001** [U]: When `modok init --project {slug} --repo {path}` is run, the system shall install a post-commit hook in the project repo that runs ingestion after any commit touching registered ingestion paths.
- [x] **SI-HOOK-002** [U]: The post-commit hook shall exit immediately without running ingestion when no changed file in the commit matches the project's registered ingestion paths.
- [x] **SI-HOOK-003** [U]: When a post-commit hook already exists in the target repo, `modok init` shall append a clearly marked MODOK section rather than overwriting the existing hook.
- [x] **SI-HOOK-004** [U]: When a MODOK section already exists in the post-commit hook, `modok init` shall replace only that section, leaving all other hook content unchanged.

---

## LLM Proposal Pass

- [x] **SI-LLM-001** [U]: When `--fix` is specified and a doc is missing required metadata fields after the mechanical parse completes, the system shall invoke the LLM gateway for proposals. Without `--fix`, the system shall not invoke the LLM gateway at all — missing fields are reported as warnings only.
- [x] **SI-LLM-002** [U]: When `--fix` is not specified, the system shall not invoke the LLM gateway and shall not modify any source file; missing required fields are emitted as structured warnings.
- [x] **SI-LLM-003** [U]: When `--fix` is specified, the system shall call `verify_proposal` on the returned `MetadataProposal` before writing anything to the doc file or to Quine.
- [x] **SI-LLM-004** [U]: When `--fix` is specified without `--strict`, the system shall write only `valid_fields` from the `VerificationResult` to the doc frontmatter; each field in `rejected_fields` shall produce a structured warning in the ingestion report. Ingestion continues for the doc using the partially-updated frontmatter.
- [x] **SI-LLM-005** [U]: When `--fix --strict` is specified and `VerificationResult.is_valid` is `False` after all repair attempts, the system shall write nothing to the doc file or Quine for that doc and shall emit a structured error per rejected field. The doc's node count contribution to the ingestion report shall be zero.
- [x] **SI-LLM-006** [U]: When `--fix --dry-run` is specified, the system shall make the LLM proposal call (and repair call if verification fails), print the proposed patch and verification result to stdout, and write nothing to any file or Quine. The command shall exit `0` regardless of whether the proposal passed verification.
- [x] **SI-LLM-007** [U]: When `--fix --emit-counterexamples` is specified, the system shall write a YAML counterexample file to the path configured in `llm.counterexample_fixture_dir` for every doc where one or more fields were rejected (from either the initial or repair pass). The file shall be named `{doc_slug}_{iso_timestamp}.yaml` and shall contain `case_id`, `input`, `expected`, `actual`, and `counterexamples` sections. When `llm.counterexample_fixture_dir` is not configured, the system shall exit `1` with the message "`--emit-counterexamples` requires `llm.counterexample_fixture_dir` in config".
- [x] **SI-LLM-008** [U]: When running in non-interactive mode (`sys.stdin.isatty()` returns `False`), the system shall suppress all LLM proposal calls including the repair attempt, emit a single warning to stderr, and continue ingestion without modifying any doc file.
- [x] **SI-LLM-009** [U]: When `propose_metadata` raises `LLMResponseError` or `LLMUnavailableError`, the system shall catch the exception, emit a structured warning, and skip the LLM proposal pass for that doc — it shall not halt ingestion of other files.
- [x] **SI-LLM-010** [U]: After writing `valid_fields` to the doc frontmatter, the system shall re-run stages 2–5 of the mechanical parser (frontmatter parse, reference validation, MODOK block parsing, heading extraction) on the updated file before writing any nodes or edges to Quine. Stage 1 (file discovery) and stage 6 (commit SHA) are not re-run.

---

## Ingestion Report

- [x] **SI-RPT-001** [U]: The system shall emit a structured ingestion report after every run containing: docs processed, nodes written, edges written, warnings count, errors count, LLM proposals count, duration, files ignored (matched ignore patterns — SI-DISC-002), and files skipped (present but no `modok:` frontmatter — SI-DISC-003). Ignored and skipped are separate counts.
- [x] **SI-RPT-002** [U]: Warnings shall not halt ingestion; errors shall halt ingestion for the affected file and allow ingestion of remaining files to continue.
