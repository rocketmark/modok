# Code Map Extractor Specs

Specs for `modok.code_map` — the deterministic, LLM-free repo extraction pass that produces `.modok/code-map.yml`.

LLD: `docs/llds/code-map.md`

---

## Test Level Convention

- **[U]** — Unit test with mocked dependencies.
- **[P]** — Property test (`hypothesis`). Implies [U].
- **[I]** — Integration test against a real (tmp_path) directory tree.

---

## Command Interface

- [ ] **CM-CMD-001** [U]: The system shall expose a CLI command `modok extract-code-map` accepting `--project <slug>`, optional `--repo <path>`, and optional `--output <path>`.
- [ ] **CM-CMD-002** [U]: When `--repo` is omitted, the system shall use the project's configured repo path from `~/.modok/config.toml`. When the project slug is not found in config, the command shall exit non-zero with a clear error.
- [ ] **CM-CMD-003** [U]: When the repo path does not exist or is not a directory, the command shall exit non-zero with the message `not a directory: <path>`.
- [ ] **CM-CMD-004** [U]: On success, the command shall print one summary line to stdout: `Extracted code map: N files (S source, T test, C config, D docs) → <output_path>`.
- [ ] **CM-CMD-005** [U]: When `.modok/` is not present in the repo root's `.gitignore` file (or no `.gitignore` exists), the command shall print to stdout after the summary: `Tip: add '.modok/' to .gitignore to exclude the code map from version control.` This hint is printed at most once per run and is suppressed if `.modok/` is already covered.

---

## Repo Scanning

- [ ] **CM-SCAN-001** [I]: The system shall walk the repo root recursively and include all regular files not excluded by ignore rules.
- [ ] **CM-SCAN-002** [U]: The system shall skip files and directories matching the ignore patterns: `.git/**`, `node_modules/**`, `bin/**`, `obj/**`, `dist/**`, `build/**`, `coverage/**`, `.vs/**`, `__pycache__/**`, `*.pyc`, `*.pyo`, `.env`, `*.key`, `*.pem`, `*.pfx`, `.modok/**`.
- [ ] **CM-SCAN-003** [U]: The system shall skip files whose paths match patterns in the repo-root `.gitignore`. Nested `.gitignore` files are not processed.
- [ ] **CM-SCAN-004** [U]: Symlinks shall not be followed. A symlink shall be recorded in the output with `role: ignored` and `symlink: true`.
- [ ] **CM-SCAN-005** [U]: Files larger than 10 MB shall be recorded with `role: ignored` and `skipped: "file too large"`. No hash, line count, or symbol extraction is performed.
- [ ] **CM-SCAN-006** [P]: The scanner shall be deterministic — the same directory state always produces the same ordered file list.

---

## Language Detection

- [ ] **CM-LANG-001** [U]: The system shall detect language from file extension using a static mapping. No file content is read for language detection.
- [ ] **CM-LANG-002** [U]: Files with unrecognised extensions shall be assigned `language: unknown`.
- [ ] **CM-LANG-003** [P]: Language detection shall be a pure function of the file path — same path always produces the same language value.

---

## Role Classification

- [ ] **CM-ROLE-001** [U]: Role shall be assigned using the following priority order: ignored → generated → test → config → docs → source. The first matching rule wins.
- [ ] **CM-ROLE-002** [U]: A file shall be classified as `generated` if its name matches `*.g.cs`, `*.designer.cs`, or `*.generated.h`, or if it is under a directory named `Generated` or `Intermediate`.
- [ ] **CM-ROLE-003** [U]: A file shall be classified as `test` if its name starts with `test_`, ends with `_test`, starts with `Test`, or ends with `Tests`, or if it resides in a directory named `tests`, `test`, `Tests`, or `Test`.
- [ ] **CM-ROLE-004** [U]: A file shall be classified as `config` if its extension is one of: `.toml`, `.yaml`, `.yml`, `.json`, `.ini`, `.config`, `.csproj`, `.sln`, `.uplugin`, `.uproject`, or if its name matches `.env.*`.
- [ ] **CM-ROLE-005** [U]: A file shall be classified as `docs` if its extension is `.md` or `.mdx`.
- [ ] **CM-ROLE-006** [U]: All other regular files not matched by the above rules shall be classified as `source`.

---

## File Facts

- [ ] **CM-FILE-001** [U]: Files excluded by pattern-based ignore rules (CM-SCAN-002, CM-SCAN-003) are silently dropped and do not appear in the output. Symlinks (CM-SCAN-004) and oversized files (CM-SCAN-005) are recorded with `role: ignored` and minimal fields (`path`, plus `symlink: true` or `skipped: "file too large"`); no hash, line count, or symbol extraction is performed for them. All other files are recorded with: `path` (repo-relative, POSIX), `sha256` (hex digest of file contents), `language`, `role`, and `line_count`.
- [ ] **CM-FILE-002** [U]: `line_count` shall be omitted for files whose language is `unreal_asset` or `unreal_project`.
- [ ] **CM-FILE-003** [U]: All file paths in the output shall be repo-relative and use forward slashes with no leading `./`.
- [ ] **CM-FILE-004** [P]: The SHA-256 hash of a file shall be identical across repeated extractions of the same file content.

---

## Python Symbol Extraction

- [ ] **CM-SYM-001** [U]: For files with `language: python` and `role: source`, the system shall parse the file with `ast.parse()` and extract: top-level functions (name, kind: function, line_start, line_end), classes (name, kind: class, line_start, line_end), and methods within classes (name, kind: method, parent: class name, line_start, line_end). Python files with `role: test` are excluded from symbol extraction; import resolution for coverage detection is handled separately by CM-TEST-003.
- [ ] **CM-SYM-002** [U]: For files with `language: python` and `role: source`, the system shall extract imports: `import X` as `{module: X, names: []}` and `from X import Y, Z` as `{module: X, names: [Y, Z]}`.
- [ ] **CM-SYM-003** [U]: If reading the file raises `UnicodeDecodeError`, the system shall record `parse_error: "encoding"` on the file entry and skip symbol and import extraction. Scanning continues with the next file.
- [ ] **CM-SYM-004** [U]: If `ast.parse()` raises `SyntaxError`, the system shall record `parse_error: "syntax"` on the file entry and skip symbol and import extraction. Scanning continues with the next file.
- [ ] **CM-SYM-005** [U]: The `symbols` and `imports` keys shall be omitted entirely from non-Python file entries.
- [ ] **CM-SYM-006** [P]: Symbol extraction shall be a pure function of file content — same content always produces the same symbol list.

---

## Test Coverage Detection

- [ ] **CM-TEST-001** [U]: For each test file, the system shall attempt mirrored-path coverage: strip the leading test directory prefix and the `test_` / `_test` name prefix/suffix, then search the scanned file set for any file with the resulting base name. All matches are recorded as `covers` entries with `method: mirrored_path`.
- [ ] **CM-TEST-002** [U]: When multiple source files match the mirrored base name, all shall be recorded. No disambiguation is performed.
- [ ] **CM-TEST-003** [U]: For Python test files, the system shall additionally resolve local imports against the scanned file set. Any local import that resolves to a source file shall be recorded as a `covers` entry with `method: import_reference`.
- [ ] **CM-TEST-004** [U]: Non-Python test files receive only mirrored-path coverage detection. Import resolution is not performed.
- [ ] **CM-TEST-005** [U]: A test file with no coverage matches shall be recorded with `covers: []`.

---

## Output Artifact

- [ ] **CM-OUT-001** [I]: The system shall write a valid YAML file to the output path. The file shall be parseable by `yaml.safe_load` without error.
- [ ] **CM-OUT-002** [U]: The output shall contain the fields: `project`, `repo_root`, `generated_at` (ISO 8601 UTC), `git_commit` (HEAD SHA or null), and `files`.
- [ ] **CM-OUT-003** [P]: The `files` list shall be sorted by `path` lexicographically. `symbols` within a file shall be sorted by `line_start`. `imports` within a file shall be sorted by `module`. `covers` entries within a test file shall be sorted by `path`.
- [ ] **CM-OUT-004** [U]: When the repo root is not a git repository, `git_commit` shall be `null`.
- [ ] **CM-OUT-005** [I]: Running `extract-code-map` twice on an unchanged repo shall produce byte-identical output (excluding the `generated_at` timestamp field).
- [ ] **CM-OUT-006** [U]: When the scan produces no file entries of any kind (no regular files, no symlinks, no oversized files), the output shall contain `files: []`. The command shall exit zero.

---

## Auto-generation from `modok ingest`

- [ ] **CM-AUTO-001** [U]: When `modok ingest` is invoked and `.modok/code-map.yml` does not exist, the system shall print a warning to stderr and run `extract-code-map` before proceeding.
- [ ] **CM-AUTO-002** [U]: When `modok ingest --strict` is invoked and `.modok/code-map.yml` does not exist, the command shall exit non-zero with the message `No code map found. Run 'modok extract-code-map --project <slug>' first.` No auto-generation shall occur.
- [ ] **CM-AUTO-003** [U]: When `modok ingest --no-code-map` is invoked, the system shall skip the code map check entirely and proceed without code-map validation.
- [ ] **CM-AUTO-004** [U]: When auto-generation fails (non-zero exit from `extract-code-map`), `modok ingest` shall abort with a non-zero exit and surface the extraction error.

---

## Pending Cascade

CM-AUTO-001 through CM-AUTO-004 describe behaviour that belongs to the ingestion-pipeline segment (`docs/llds/ingestion-pipeline.md`, `docs/specs/ingestion-pipeline.md`). Those documents do not yet reference the code map. This cascade is deferred until the code map implementation is complete.
