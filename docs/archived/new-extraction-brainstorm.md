# MODOK Deterministic Code Extraction Before Doc Ingestion

## Goal

Implement a deterministic repo feature extraction layer for MODOK.

The purpose is to make MODOK's graph more trustworthy by extracting stable code facts first, then ingesting docs, tickets, and resolutions against that known code universe.

Core principle:

> Code extraction tells MODOK what exists.
> Registries tell MODOK what names mean.
> Docs explain why those things matter.
> Tickets describe where they failed.
> Resolutions describe how they were fixed.

This should reduce reliance on LLM inference and make the graph easier to validate, debug, and explain.

---

## Existing MODOK Principles To Preserve

This implementation should preserve the existing MODOK ingestion philosophy:

1. The parser is mechanical.
2. The LLM is only a proposer.
3. Explicit metadata is truth.
4. Invalid references fail loudly.
5. Ingestion is idempotent.
6. No LLM output is written to Quine without validation.
7. Registries are source-of-truth metadata and live in the repo.
8. The graph should contain explainable, validated relationships.

This code extraction layer should strengthen those principles rather than replace them.

---

## High-Level Pipeline

Current ingestion should be reshaped around this order:

```text
Repo checkout
  ↓
1. Deterministic code extraction
  ↓
2. Registry validation
  ↓
3. Doc ingestion
  ↓
4. Ticket / resolution ingestion
  ↓
5. Quine graph write
```

The important change is that docs should not be the first source of truth for source files, modules, symbols, tests, routes, config keys, or error strings.

Docs should make claims about an already-known code universe.

---

## Proposed Command

Add or formalize a command like:

```bash
modok extract-code-map --project <project_slug> <repo_path>
```

or use the existing ingestion-style command:

```bash
modok ingest-code-map --project <project_slug> ./src ./tests
```

This command should produce a deterministic intermediate artifact before writing to Quine.

Example artifact path:

```text
.modok/code-map.yml
```

or, if we want it versioned/reviewable:

```text
modok/code-map.yml
```

Recommendation: start with a generated file under `.modok/` and later decide whether to support a checked-in version.

---

## Deterministic Code Map Artifact

The code extraction pass should write a structured YAML artifact similar to:

```yaml
project: stagehand
repo_root: /path/to/repo
generated_at: "2026-05-03T00:00:00Z"
git_commit: "<current-or-last-known-commit>"

files:
  - path: src/modok/ingestion/parser.py
    sha256: "<file hash>"
    language: python
    role: source
    module_slug: ingestion
    feature_slug: ingestion
    symbols:
      - name: parse_frontmatter
        kind: function
        line_start: 42
        line_end: 88
      - name: IngestionResult
        kind: class
        line_start: 12
        line_end: 28
    imports:
      - yaml
      - pathlib.Path
    string_literals:
      - "modok:"
      - "feature"
    error_literals:
      - "missing required field"
    config_keys:
      - llm.cegis_fix_enabled

tests:
  - path: tests/ingestion/test_parser.py
    sha256: "<file hash>"
    language: python
    covers_files:
      - src/modok/ingestion/parser.py
    test_cases:
      - name: test_rejects_unknown_feature_slug
        line_start: 18
        line_end: 34
```

The artifact should be deterministic:

- Same repo state produces the same code map.
- Sort all maps/lists by stable keys.
- Normalize paths to repo-relative POSIX-style paths.
- Do not include absolute paths unless needed for diagnostics.
- Do not include nondeterministic timestamps in fields used for hashing or tests.
- Generated timestamps are okay only as metadata.
- Snapshot tests should be able to ignore or normalize generated metadata.

---

## What To Extract

Start with facts that are syntactic, mechanical, and explainable.

### File Facts

Extract:

- repo-relative path
- file hash
- language
- role: source, test, config, docs, generated, ignored
- line count
- last commit SHA if available
- whether file is ignored by MODOK rules

Example:

```yaml
files:
  - path: src/modok/ingestion/parser.py
    sha256: "<hash>"
    language: python
    role: source
    line_count: 240
    last_commit_sha: "<sha-or-null>"
```

### Symbol Facts

Use deterministic parsers where possible.

For Python, use `ast`.

For TypeScript/JavaScript, either use tree-sitter or add a simple parser layer later.

For initial implementation, Python-only is acceptable if MODOK itself is Python.

Extract:

- classes
- functions
- methods
- constants
- line ranges
- parent symbol if nested
- exported/public marker if available

Example:

```yaml
symbols:
  - name: DiagnosticRetrievalEngine
    kind: class
    line_start: 20
    line_end: 140

  - name: retrieve
    kind: method
    parent: DiagnosticRetrievalEngine
    line_start: 44
    line_end: 91
```

### Import Facts

Extract imports/includes/requires.

Example:

```yaml
imports:
  - module: pathlib
    names:
      - Path
  - module: modok.registry
    names:
      - Registry
```

Where possible, resolve local imports to repo files.

Example:

```yaml
resolved_imports:
  - from: src/modok/ingestion/parser.py
    to: src/modok/registry.py
    method: python_import_resolution
```

### Test Facts

Extract:

- test files
- test functions/classes
- source files covered by naming convention
- source files covered by imports
- source files covered by explicit MODOK annotation

Example:

```yaml
tests:
  - path: tests/ingestion/test_parser.py
    covers_files:
      - path: src/modok/ingestion/parser.py
        method: mirrored_path
      - path: src/modok/registry.py
        method: import_reference
```

### Error Facts

Extract mechanically visible error information:

- raised exception messages
- logged error strings
- known error code constants
- string literals that match registered error signatures
- strings with prefixes configured in registry

Example:

```yaml
error_literals:
  - text: "missing required field"
    line: 54
    kind: exception_message

  - text: "LLM response failed validation"
    line: 88
    kind: log_message
```

Do not infer meaning from arbitrary prose-like strings. Extract only where the syntactic context is reliable.

### Config Facts

Extract:

- environment variable names
- config keys
- TOML/YAML keys
- feature flags

Example:

```yaml
config_keys:
  - key: llm.cegis_fix_enabled
    line: 32
    source: toml_key_access

env_vars:
  - name: MODOK_LLM_API_KEY
    line: 71
```

### Entry Point Facts

Eventually extract:

- CLI commands
- HTTP routes
- queue handlers
- scheduled jobs
- plugin entry points

For the first pass, CLI commands may be enough.

Example:

```yaml
cli_commands:
  - name: ingest-code-map
    handler: modok.cli.ingest_code_map
    file: src/modok/cli.py
    line: 120
```

---

## Feature Mapping Rules

Feature ownership must be deterministic.

Do not ask the LLM:

> What feature does this file belong to?

Instead, use explicit rules.

Priority order:

```text
1. Explicit source annotation
2. Feature registry source_roots
3. Module registry source_roots
4. Build/package ownership
5. CODEOWNERS-style ownership
6. Path convention
7. Test/doc cross-reference
8. LLM proposal only, never trusted automatically
```

---

## Registry Extensions

Extend `registries/features.yml` and `registries/modules.yml` to support deterministic source mapping.

Example:

```yaml
features:
  retrieval:
    name: Diagnostic Retrieval
    product_area: modok-core
    source_roots:
      - src/modok/retrieval
    test_roots:
      - tests/retrieval
    route_prefixes:
      - /retrieve
    cli_commands:
      - retrieve
    error_prefixes:
      - DRE_
    aliases:
      - debug packet
      - retrieval engine

modules:
  ingestion:
    name: Ingestion Pipeline
    source_roots:
      - src/modok/ingestion
    test_roots:
      - tests/ingestion
```

Mapping rule:

```text
If a file path is under feature.source_roots, link File → Feature.
If a file path is under module.source_roots, link File → Module.
If a module belongs to a feature, link Module → Feature.
If a test path is under module.test_roots, link TestFile → Module.
If an error prefix matches feature.error_prefixes, link ErrorSignature → Feature.
```

All ambiguous mappings should become warnings, not silent guesses.

---

## Source Annotations

Some repos are not organized cleanly by feature. Support constrained source annotations.

Python:

```python
# modok: feature=retrieval module=diagnostic-retrieval
class DiagnosticRetrievalEngine:
    ...
```

TypeScript:

```ts
// modok: feature=ticket-ingestion module=ticket-parser
export function parseTicket(...) {
  ...
}
```

C#:

```csharp
// modok: feature=invoice-export module=billing-validation
public sealed class DateRangeValidator { ... }
```

Rules:

- Annotation syntax must be small and strict.
- Unknown feature/module slugs are errors.
- Annotation values must validate against registries.
- An annotation beats path-based inference.
- Conflicting annotations are errors.
- Multiple features on one file should be allowed only if explicitly supported.

Suggested parser grammar:

```text
modok: feature=<slug> module=<slug>
modok: features=[<slug>,<slug>] module=<slug>
```

Start with the simple single-feature form only:

```text
modok: feature=<slug> module=<slug>
```

---

## Graph Model Additions

Add code-side graph nodes and edges.

Suggested nodes:

```text
File
Symbol
ExternalPackage
ConfigKey
Route
CliCommand
TestCase
```

Suggested edges:

```text
Feature IMPLEMENTED_BY Module
Module CONTAINS_FILE File
File DECLARES Symbol
File IMPORTS File
File IMPORTS_PACKAGE ExternalPackage
File EMITS ErrorSignature
File READS_CONFIG ConfigKey
File DEFINES_ROUTE Route
File DEFINES_COMMAND CliCommand
TestFile COVERS File
TestCase TESTS Symbol
Doc REFERENCES File
Doc REFERENCES Symbol
KnownIssue AFFECTS File
KnownIssue AFFECTS Symbol
Fix CHANGED File
ResolutionEvent CHANGED File
```

Important: edge creation should preserve MODOK's existing discipline:

- no dangling references
- validate before write
- deterministic node IDs
- idempotent upserts
- stable edge ordering

---

## Code Extraction Before Docs

Doc ingestion should run after code extraction.

Docs currently include claims like:

```yaml
modok:
  feature: retrieval
  modules:
    - diagnostic-retrieval
  source_files:
    - src/modok/retrieval/engine.py
  test_files:
    - tests/retrieval/test_engine.py
```

After code extraction, doc ingestion can validate:

```text
Does src/modok/retrieval/engine.py exist?
Is it in the code map?
Does it belong to the claimed module?
Does the module belong to the claimed feature?
Does the test file exist?
Does the test file map to the source file?
Are referenced symbols present?
Are referenced error signatures present?
```

Docs should become claims over known code facts.

Do not require every doc to map to code. HLDs, runbooks, incident notes, and conceptual docs may not have source files.

Rule:

```text
Any doc claim about code must validate against the extracted code map.
Not every doc must make claims about code.
```

---

## Confidence / Provenance Model

For code extraction, prefer deterministic provenance over probabilistic confidence.

Example:

```yaml
relationship:
  from: file:src/modok/retrieval/engine.py
  type: BELONGS_TO_MODULE
  to: module:retrieval
  source: registry.source_root
  confidence: 1.0
```

Suggested confidence/provenance levels:

```text
1.00 explicit source annotation
1.00 registry source_root
0.95 build/package manifest
0.90 CODEOWNERS / ownership rule
0.85 path convention
0.75 test import / source import heuristic
<0.75 proposal only; do not write automatically
```

But prefer `source` / `method` fields over confidence where possible.

Examples:

```yaml
method: explicit_annotation
method: registry_source_root
method: module_source_root
method: mirrored_test_path
method: import_resolution
method: path_convention
method: llm_proposal
```

LLM proposals should never be written as trusted code facts without validation or explicit acceptance.

---

## Interaction With LLM Gateway

The LLM Gateway should not become the repo feature extractor.

Allowed LLM role:

- propose missing metadata
- propose possible feature mapping for unmapped files
- propose similarity between issues
- propose doc metadata repairs

Forbidden LLM role:

- directly write code facts to Quine
- directly mutate docs without validation
- silently infer feature ownership
- create graph edges without deterministic support

If the LLM proposes feature ownership for a file, persist it only as a pending proposal:

```yaml
pending_proposals:
  - kind: file_feature_mapping
    file: src/modok/retrieval/engine.py
    proposed_feature: retrieval
    evidence: "File path and class names mention retrieval."
    confidence: 0.72
    status: pending_review
```

This can later become a registry update or source annotation after human approval.

---

## Implementation Plan

### Phase 1: Code Map Skeleton

Implement:

```bash
modok extract-code-map --project <project> <repo_root>
```

Output:

```text
.modok/code-map.yml
```

Extract:

- files
- hashes
- language
- role
- ignored/generated status
- basic Python symbols via `ast`
- basic Python imports via `ast`
- test files by path convention

Acceptance criteria:

- command is deterministic
- output is sorted
- repeated run with same repo produces same artifact
- ignored paths are skipped
- invalid repo path fails clearly

---

### Phase 2: Registry-Based Mapping

Extend registries:

```yaml
features:
  <slug>:
    source_roots: []
    test_roots: []

modules:
  <slug>:
    source_roots: []
    test_roots: []
```

Implement mapping:

- File → Feature
- File → Module
- TestFile → Module
- TestFile → File where mirrored path or import supports it

Acceptance criteria:

- unknown feature/module slugs fail validation
- overlapping source roots produce warnings or errors
- exact annotation beats source_root mapping
- mapping results include `method`

---

### Phase 3: Doc Validation Against Code Map

Update doc ingestion so `source_files` and `test_files` validate against `.modok/code-map.yml`.

Rules:

- source file claimed by doc but absent from code map: warning or error depending on strictness
- source file exists but belongs to different feature: warning or error
- module claim conflicts with code map: warning or error
- test file exists but does not map to claimed source file: warning
- doc without source_files is allowed depending on doc type

Acceptance criteria:

- docs no longer validate source files only by checking disk existence
- docs validate against extracted code map
- ingestion report shows code map validation failures clearly

---

### Phase 4: Quine Write

Add graph writes for deterministic code facts.

Nodes:

```text
File
Symbol
ConfigKey
ExternalPackage
CliCommand
TestCase
```

Edges:

```text
Module CONTAINS_FILE File
File DECLARES Symbol
File IMPORTS File
File IMPORTS_PACKAGE ExternalPackage
File READS_CONFIG ConfigKey
File DEFINES_COMMAND CliCommand
TestFile COVERS File
Doc REFERENCES File
Doc REFERENCES Symbol
```

Acceptance criteria:

- graph write is idempotent
- node IDs are stable
- stale file properties are updated on re-ingest
- deleted files are handled intentionally, either by tombstone or removal policy
- no duplicate nodes/edges on repeated ingestion

---

### Phase 5: Source Annotations

Support comments like:

```python
# modok: feature=retrieval module=diagnostic-retrieval
```

Implement:

- strict parser
- registry validation
- conflict detection
- mapping override behavior

Acceptance criteria:

- invalid slug fails loudly
- conflicting annotations fail loudly
- annotation mapping uses `method: explicit_annotation`
- annotation mapping has confidence/provenance 1.0

---

## Design Constraints

Follow these constraints:

1. Code extraction must be deterministic.
2. LLMs must not be required for code extraction.
3. LLMs may propose, but not write.
4. Registries remain source-of-truth for feature/module names.
5. Docs validate against the code map.
6. Repeated ingestion must be idempotent.
7. Invalid references should fail loudly.
8. Ambiguous mappings should become warnings or pending proposals.
9. The output artifact should be diffable and testable.
10. Quine writes should happen only after validation.

---

## Testing Plan

Add tests for:

### Determinism

- same repo input produces same code map
- output ordering is stable
- hashes are stable
- generated timestamp does not break snapshot tests

### Python Extraction

- functions extracted
- classes extracted
- methods extracted
- imports extracted
- nested functions handled intentionally
- syntax errors produce structured warnings

### Mapping

- source_root maps file to module
- source_root maps file to feature
- annotation overrides path mapping
- unknown annotation slug fails
- overlapping source roots are detected
- test root maps tests to module
- mirrored path maps test to source

### Doc Validation

- doc source_file exists in code map
- doc source_file missing from code map
- doc feature conflicts with code map feature
- doc module conflicts with code map module
- doc test_file does not cover claimed source file

### Graph Write

- repeated code-map ingestion does not duplicate nodes
- changed file hash updates File node
- deleted file behavior is explicit
- Symbol node IDs are stable across runs

---

## Suggested Internal Modules

Possible file layout:

```text
modok/
  code_map/
    __init__.py
    scanner.py          # repo walk, ignore rules, file hashes
    languages.py        # language detection
    python_ast.py       # Python symbol/import extraction
    annotations.py      # modok source comment parser
    mapper.py           # registry/path/annotation feature mapping
    schema.py           # pydantic models for code map
    writer.py           # YAML writer with stable ordering
    validator.py        # validates code map against registries
    ingest.py           # Quine write orchestration
```

CLI:

```text
modok/cli.py
  extract-code-map
  ingest-code-map
```

---

## Open Design Decisions

Decide these during implementation:

1. Should `.modok/code-map.yml` be runtime-only or checked into the repo?
2. Should missing doc source_files be warnings or errors by default?
3. How should deleted files be represented in Quine?
4. Should multiple features per file be supported in v1?
5. Should tree-sitter be introduced now or later?
6. Should code extraction be language-specific at first, or generic file-level first?
7. Should code map ingestion run automatically before doc ingestion?
8. Should `modok ingest-docs` fail if no code map exists, or generate one automatically?

Recommended initial answers:

1. Runtime-only under `.modok/` for now.
2. Warning by default, error in `--strict`.
3. Mark stale/tombstoned rather than hard-delete initially.
4. Single feature per file in v1.
5. Use Python `ast` first; add tree-sitter later.
6. Python-specific symbols first, generic file-level for all languages.
7. Yes, run code extraction before docs when repo path is configured.
8. Generate automatically if missing, unless `--no-code-map` is passed.

---

## One-Sentence Summary

Implement MODOK code extraction as a deterministic base layer: extract repo facts first, map files to features/modules through registries and explicit annotations, then ingest docs as validated claims over that known code universe.
