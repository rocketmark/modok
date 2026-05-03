# Registry Proposal Engine Specs

Specs for `modok.registry` — the LLM-assisted bootstrapping pass that proposes `features.yml`, `modules.yml`, and `errors.yml` from a project's existing docs.

LLD: `docs/llds/registry-proposal.md`

---

## Test Level Convention

See `docs/testing-standard.md` for full definitions.

- **[U]** — Unit test with mocked dependencies.
- **[P]** — Property test (`hypothesis`). Implies [U].
- **[C]** — Contract test against live Quine instance. Implies [U].

---

## Doc Discovery

- [ ] **RP-DISC-001** [U]: The system shall discover all `.md` and `.mdx` files under `{repo_root}` recursively, excluding the `registries/` directory.
- [ ] **RP-DISC-002** [U]: The system shall skip files matching any ignore pattern (`.git/**`, `node_modules/**`, `bin/**`, `obj/**`, `dist/**`, `build/**`, `coverage/**`, `.vs/**`, `.env`, `*.key`, `*.pem`, `*.pfx`).
- [ ] **RP-DISC-003** [U]: `.yaml` and `.yml` files shall not be included in discovery — only `.md` and `.mdx` are eligible.

---

## Section Parser

- [ ] **RP-PARSE-001** [U]: The system shall strip YAML frontmatter (content between opening and closing `---` delimiters) before parsing sections.
- [ ] **RP-PARSE-002** [U]: The system shall skip H1 headings and shall not treat them as section boundaries.
- [ ] **RP-PARSE-003** [U]: The system shall split each file body on H2 (`## `) headings, producing one `Section` per H2 and its following content up to the next H2 or end of file.
- [ ] **RP-PARSE-004** [U]: The system shall skip sections whose body is empty after stripping whitespace. Sections whose body contains only links or list items shall not be skipped — they are sent to the LLM as-is.
- [ ] **RP-PARSE-005** [U]: A file shall be counted as **processed** if it contributed at least one non-empty section. A file shall be counted as **skipped** if it has no H2 headings or all section bodies are empty.
- [ ] **RP-PARSE-006** [P]: The parser shall never call the LLM gateway. `parse_sections` is a pure function with no I/O beyond reading the file.

---

## Per-Section Enrichment

- [ ] **RP-ENRICH-001** [U]: For each non-empty section, the system shall call `enrich_section(section)` via the LLM gateway and receive an `EnrichSectionResult` containing lists of candidates for: `features`, `modules`, `error_signatures`, `known_issues`, `failure_modes`, `decisions`, `observation_events`.
- [ ] **RP-ENRICH-002** [U]: The system shall process sections sequentially — one LLM call at a time. The system shall not issue concurrent enrichment calls.
- [ ] **RP-ENRICH-003** [U]: Before processing begins, the system shall print to stderr: `"Found N sections across M docs"` where N is the total section count and M is the total eligible doc count.
- [ ] **RP-ENRICH-004** [U]: When `enrich_section` raises `LLMUnavailableError` or `LLMResponseError` for a section, the system shall emit a structured warning to stderr identifying the section heading and doc path, record the section as failed, and continue processing remaining sections.
- [ ] **RP-ENRICH-009** [U]: A section for which `enrich_section` returns successfully with all-empty lists shall be counted as processed, not failed. A section shall be counted as failed only when `enrich_section` raises `LLMUnavailableError` or `LLMResponseError`.
- [ ] **RP-ENRICH-010** [U]: After each section completes, the system shall print one line to stderr: `N/total` on success (e.g. `1/495`), or `N/total FAILED: 'heading' (reason)` on failure. One line per section; no separate warning lines for failures.
- [ ] **RP-ENRICH-005** [U]: The system shall use `timeout_propose_registry` from `[llm]` in config (default 60s) as the per-section timeout. When the key is absent, the system shall fall back to `timeout_seconds`.
- [ ] **RP-ENRICH-006** [U]: `enrich_section` shall use the frozen system prompt constant `ENRICH_SECTION_SYSTEM` from `modok.llm.prompts` and shall not construct prompts dynamically beyond interpolating the section heading and body.
- [ ] **RP-ENRICH-007** [U]: When `backend="local"`, `enrich_section` shall use the Ollama native API (`/api/chat`) with `think: false`, `format: json`, and `num_ctx: 8192`. When `backend="remote"`, it shall use the OpenAI-compatible `/v1/chat/completions` endpoint. The default backend for registry proposal is `"local"`.
- [ ] **RP-ENRICH-008** [U]: `enrich_section` shall never call `upsert_node`, `write_edge`, or any Quine client method. It returns a result struct only.

---

## Normalisation Pass

- [ ] **RP-NORM-001** [P]: After all sections are processed, the system shall merge all per-section candidates into one flat list per node type, deduplicated by exact string match, before writing to `.raw.yml` checkpoint files.

---

## Slug Derivation

- [ ] **RP-SLUG-001** [U]: The system shall derive registry keys from candidate strings via `slugify(text)`, which: strips leading Unicode non-ASCII symbol characters and whitespace; replaces `(s)` with `s`; strips remaining parentheses and punctuation; lowercases; replaces runs of non-alphanumeric characters with a single `-`; strips leading and trailing `-`; truncates to 40 characters at a word boundary.
- [ ] **RP-SLUG-002** [P]: `slugify` shall be a pure function — identical inputs always produce identical outputs with no side effects.
- [ ] **RP-SLUG-003** [P]: When two distinct candidate strings produce the same slug, the system shall compare their names after lowercasing and stripping whitespace. If the normalised names are identical, the system shall keep the entry with the longer original `name` string and emit a stderr warning: `"merged duplicate slug '<slug>': kept '<name>'"`.
- [ ] **RP-SLUG-004** [U]: When two candidates produce the same slug and their lowercased, whitespace-stripped names differ, the system shall keep both: the second entry shall use the slug suffixed with `-2`, and the system shall emit a warning: `"slug collision '<slug>': could not merge — review registries/<file>.yml"`. No semantic judgment is applied — the rule is purely string equality after normalisation.
- [ ] **RP-SLUG-005** [U]: All slug collision warnings shall be emitted to stderr, not stdout.

---

## Registry Write

- [ ] **RP-WRITE-001** [U]: The system shall create `{repo_root}/registries/` if it does not exist before writing any registry file.
- [ ] **RP-WRITE-003** [U]: `features.yml` shall contain a top-level `features:` key whose values are objects with `name` and `description` fields, keyed by slug.
- [ ] **RP-WRITE-004** [U]: `modules.yml` shall contain a top-level `modules:` key whose values are objects with `name` and `description` fields, keyed by slug.
- [ ] **RP-WRITE-005** [U]: `errors.yml` shall contain a top-level `errors:` key whose values are objects with `normalized_error` and `description` fields, keyed by slug.
- [ ] **RP-WRITE-006** [P]: The system shall never write a registry file that fails to parse as valid YAML.
- [ ] **RP-WRITE-007** [U]: The system shall not interact with Quine at any point during the proposal pass — no `upsert_node`, `write_edge`, or `ping` calls are made.
- [ ] **RP-WRITE-008** [U]: After all sections are processed, `modok init --assisted` shall write merged candidates to `features.raw.yml`, `modules.raw.yml`, and `errors.raw.yml` in `{repo_root}/registries/`. These checkpoint files shall use the same YAML structure as the final files. The directory shall be created if it does not exist. Existing `.raw.yml` files shall be overwritten entirely.
- [ ] **RP-WRITE-009** [U]: `modok init --assisted` shall not write `features.yml`, `modules.yml`, or `errors.yml`. Writing the final registry files is the sole responsibility of `modok normalise`.

---

## Proposal Summary

- [ ] **RP-RPT-001** [U]: After writing all `.raw.yml` checkpoint files, `propose_registries` shall return a `ProposalSummary` dataclass containing: `sections_processed`, `sections_failed`, `docs_processed`, `docs_skipped`, and a `entries_written` dict mapping each registry filename to its entry count. The CLI formats and prints this struct — the engine shall not write directly to stdout.
- [ ] **RP-RPT-002** [U]: When one or more sections failed enrichment, `propose_registries` shall include each failed section (heading and doc path) in `ProposalSummary.failed_sections`. The CLI is responsible for emitting these to stderr.

---

## `modok normalise` Command

### Input Handling

- [ ] **RN-CMD-001** [U]: When one or more but not all `.raw.yml` files are absent from `{repo_root}/registries/`, the system shall emit a warning to stderr identifying each missing file and normalise only the fields whose raw files are present.
- [ ] **RN-CMD-002** [U]: When no `.raw.yml` files exist in `{repo_root}/registries/`, the system shall exit with a clear error message directing the user to run `modok init --assisted` first.

### Per-Field Normalisation

- [ ] **RN-NORM-001** [U]: The system shall send each field type (features, modules, errors) to the LLM gateway in a separate normalisation call using the frozen prompt constant `NORMALISE_REGISTRY_SYSTEM` from `modok.llm.prompts`. The system shall not combine multiple field types in a single call.
- [ ] **RN-NORM-002** [U]: Each per-field normalisation call shall use `timeout_propose_registry` from `[llm]` in config (default 60s) as its timeout.
- [ ] **RN-NORM-003** [P]: The normalisation pass shall not introduce entries whose concept did not appear anywhere in the raw merged candidates. Renaming an existing entry to a more canonical form is permitted; adding an entirely new concept is not.

### CEGIS Verification Loop

- [ ] **RN-CEGIS-001** [U]: After each per-field normalisation call, when the verifier finds no violations (output entry count ≤ input count, all error entries match `[A-Z0-9_]+` or start with a Unicode symbol, no entry has an empty or whitespace-only name), the system shall accept the normalised field and proceed to write.
- [ ] **RN-CEGIS-002** [U]: After each per-field normalisation call, when the verifier finds violations, the system shall make a repair call with the violating entries appended as counterexamples to the user message, and re-verify the repair output.
- [ ] **RN-CEGIS-003** [U]: When the CEGIS repair loop for a field is exhausted (after `cegis_max_repairs` repair attempts the field still fails verification), the system shall fall back to the raw candidates for that field, apply slug collision resolution, and emit a stderr warning identifying the field and the exhaustion. For the errors field, slug derivation on fallback shall use the `normalized_error` value as the slug source.
- [ ] **RN-CEGIS-004** [P]: The total number of LLM calls per field shall not exceed `cegis_max_repairs + 1` (one initial normalisation call plus at most `cegis_max_repairs` repair calls). `cegis_max_repairs` is a config key under `[llm]`, defaulting to 1.

### Final Registry Write

- [ ] **RN-WRITE-001** [U]: After normalisation of all fields, `modok normalise` shall write `features.yml`, `modules.yml`, and `errors.yml` to `{repo_root}/registries/`, overwriting any existing content entirely without warning. `modok normalise` is idempotent — multiple runs are expected.
- [ ] **RN-WRITE-002** [U]: `modok normalise` shall leave `.raw.yml` checkpoint files in place after writing the final registry files. It shall not delete or modify `features.raw.yml`, `modules.raw.yml`, or `errors.raw.yml`.
