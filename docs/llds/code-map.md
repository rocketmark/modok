# Code Map Extractor

## Context and Design Philosophy

The Code Map Extractor produces a deterministic, LLM-free snapshot of the repo's file structure before doc ingestion runs. Its output — `.modok/code-map.yml` — is the source of truth for what files exist in the project, what role each file plays, what language it is written in, and (for Python) what symbols and imports it contains.

The guiding principle from the HLD: **code extraction tells MODOK what exists; registries tell MODOK what names mean; docs explain why those things matter.** By running extraction first, doc ingestion can validate claims against known facts rather than accepting any claimed file path at face value.

The extractor is intentionally narrow in scope:

- No LLM involvement at any stage.
- No Quine writes. The code map is a local artifact consumed by the ingestion pipeline.
- Deterministic: the same repo state always produces the same artifact. All collections are sorted by stable keys.
- Language-agnostic at the file level. Symbol extraction is Python-only in v1, via the standard library `ast` module. Tree-sitter for other languages is deferred.

The extractor does not infer feature or module ownership. That is the registry's job. The extractor provides the raw file facts; the registry's `source_roots` and `test_roots` fields provide the mapping.

---

## Command Interface

```bash
modok extract-code-map --project <slug> [--repo <path>] [--output <path>]
```

- `--project` — required. Used for the `project` field in the artifact and to look up `repo` from config if `--repo` is omitted.
- `--repo` — optional override for the repo root path. Defaults to the project's configured repo path.
- `--output` — optional override for the output path. Defaults to `.modok/code-map.yml` under the repo root.

On success, prints a one-line summary to stdout:

```
Extracted code map: 312 files (148 source, 44 test, 23 config, 97 docs) → .modok/code-map.yml
```

On failure (invalid repo path, permission error), exits non-zero with a clear message to stderr.

---

## Repo Scanning

The scanner walks the repo root recursively using `pathlib.Path.rglob("*")`, skipping ignored paths before descending.

### Ignore rules

The following path patterns are ignored (same rules as the ingestion pipeline):

```
.git/**
node_modules/**
bin/**
obj/**
dist/**
build/**
coverage/**
.vs/**
__pycache__/**
*.pyc
*.pyo
.env
*.key
*.pem
*.pfx
.modok/**
```

The `.modok/` directory itself is excluded — the code map artifact does not describe itself.

Files listed in `.gitignore` at the repo root are also excluded. Nested `.gitignore` files are not processed in v1 (deferred).

**Symlinks** are not followed. A symlink entry is recorded with `role: ignored` and `symlink: true`. This prevents infinite loops on circular symlinks.

**Large files**: any file over 10 MB is recorded with `role: ignored` and `skipped: "file too large"`. No hash, no line count, no symbol extraction. The threshold is not configurable in v1.

All paths in the output artifact are repo-relative, POSIX-style (forward slashes, no leading `./`).

---

## Language Detection

Language is detected from file extension. The detection table is a static mapping — no content sniffing:

| Extension(s) | Language |
|---|---|
| `.py` | python |
| `.cs` | csharp |
| `.ts`, `.tsx` | typescript |
| `.js`, `.jsx` | javascript |
| `.go` | go |
| `.rs` | rust |
| `.cpp`, `.cc`, `.cxx`, `.c`, `.h`, `.hpp` | cpp |
| `.md`, `.mdx` | markdown |
| `.yaml`, `.yml` | yaml |
| `.toml` | toml |
| `.json` | json |
| `.sh`, `.bash` | shell |
| `.uasset`, `.umap` | unreal_asset |
| `.uplugin`, `.uproject` | unreal_project |
| anything else | unknown |

---

## Role Classification

Role is assigned per file using the following priority order. The first matching rule wins.

1. **ignored** — path matched an ignore rule. Not written to the code map.
2. **generated** — path matches a generated-file pattern: `*.g.cs`, `*.designer.cs`, `*.generated.h`, files under `Generated/` or `Intermediate/` directories.
3. **test** — filename matches a test convention (see Test Coverage Detection below).
4. **config** — extension is `.toml`, `.yaml`/`.yml`, `.json`, `.ini`, `.env.*`, `.config`, `.csproj`, `.sln`, `.uplugin`, `.uproject`.
5. **docs** — extension is `.md` or `.mdx`.
6. **source** — everything else that is a regular file.

---

## Symbol Extraction (Python only)

For files with `language: python` and `role: source`, the extractor runs `ast.parse()` on the file content and walks the AST to extract:

**Symbols:**
- `class` definitions: name, line_start, line_end
- `function` definitions at module level: name, line_start, line_end
- `method` definitions (functions inside a class): name, parent class name, line_start, line_end
- Constants (module-level assignments to `ALL_CAPS` names): name, line

**Imports:**
- `import X` → module: `X`, names: []
- `from X import Y, Z` → module: `X`, names: [`Y`, `Z`]

The file is first read as UTF-8. If decoding raises `UnicodeDecodeError`, the file is recorded with `parse_error: "encoding"` and no symbols or imports. If `ast.parse()` raises `SyntaxError`, the file is recorded with `parse_error: "syntax"` and no symbols or imports. Extraction continues with the next file in both cases.

Symbol extraction is skipped entirely for non-Python files. The `symbols` and `imports` keys are omitted from non-Python file entries rather than written as empty lists.

---

## Test Coverage Detection

Test files are identified by naming convention first:

- Filename starts with `test_` or ends with `_test` (Python convention)
- Filename starts with `Test` or ends with `Tests` (C# convention)
- File is in a directory named `tests/`, `test/`, `Tests/`, or `Test/`

For identified test files, coverage is inferred by **mirrored path**:

```
tests/ingestion/test_parser.py  →  src/ingestion/parser.py
```

The mirror check: strip the leading test directory prefix, strip the `test_` / `_test` prefix/suffix from the filename, then search the scanned file set for any file with that base name (regardless of directory). If multiple source files match the same base name, all matches are recorded as `covers` entries with `method: mirrored_path` — no disambiguation is attempted in v1.

For Python test files, imports are also parsed and local imports are resolved against the scanned file set. Any local import that resolves to a source file is recorded as a `covers` entry with `method: import_reference`.

Non-Python test files only get mirrored path coverage in v1.

---

## Output Artifact

The artifact is written to `.modok/code-map.yml` (or the path given by `--output`).

### Schema

```yaml
project: stagehand
repo_root: /Users/markstalzer/github/stagehand   # absolute, for diagnostics only
generated_at: "2026-05-03T12:00:00Z"             # ISO 8601 UTC; excluded from snapshot tests
git_commit: "abc123"                              # HEAD SHA, or null if not a git repo

files:
  - path: src/Tracking/TrackingEngine.cs
    sha256: "abc123..."
    language: csharp
    role: source
    line_count: 312
    # binary files (unreal_asset, unreal_project) omit line_count entirely

  - path: src/modok/ingestion/parser.py
    sha256: "def456..."
    language: python
    role: source
    line_count: 240
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
      - module: yaml
        names: []
      - module: pathlib
        names: [Path]

  - path: tests/ingestion/test_parser.py
    sha256: "ghi789..."
    language: python
    role: test
    line_count: 95
    covers:
      - path: src/modok/ingestion/parser.py
        method: mirrored_path
      - path: src/modok/registry.py
        method: import_reference
```

### Determinism requirements

- All `files` entries sorted by `path` (lexicographic, case-sensitive).
- All `symbols` within a file sorted by `line_start`.
- All `imports` within a file sorted by `module`.
- All `covers` entries within a test file sorted by `path`.
- `generated_at` and `git_commit` are metadata-only fields excluded from any snapshot or hash comparison.
- Absolute paths appear only in `repo_root`; all `path` fields are repo-relative.

---

## Auto-generation from `modok ingest`

`modok ingest` checks for `.modok/code-map.yml` before running.

**Normal mode** (default): if the file does not exist, a warning is printed to stderr (`No code map found — generating before ingestion...`), `extract-code-map` runs automatically, and ingestion proceeds. If extraction fails, ingestion aborts.

**Strict mode** (`--strict`): if the file does not exist, ingestion aborts immediately with a non-zero exit and the message `No code map found. Run 'modok extract-code-map --project <slug>' first.` No auto-generation.

**No code map** (`--no-code-map`): skips the check entirely and disables code-map validation for that run. Useful during initial project setup before any code map exists.

Empty repo (zero files after ignore filtering) produces a valid artifact with `files: []`. Ingestion proceeds normally; no doc claims can be validated against an empty code map, so all `source_files` claims produce warnings.

---

## Decisions & Alternatives

| Decision | Choice | Rationale |
|---|---|---|
| Symbol extraction scope | Python only via `ast` | `ast` is stdlib, zero deps, deterministic. C# requires tree-sitter or Roslyn; deferred. |
| Output location | `.modok/code-map.yml` (runtime, not checked in) | Keeps generated artifacts out of version control. Can be promoted to `modok/code-map.yml` later if teams want to review diffs. |
| `.gitignore` processing | Repo-root only in v1 | Nested `.gitignore` files add significant complexity; root-level covers 90% of cases. |
| Missing code map at ingest time | Auto-generate in normal mode; error in `--strict` | Normal mode reduces friction for adoption. `--strict` enforces the correct workflow in CI or gated pipelines. |
| Symlinks | Skip, record as ignored | Prevents infinite loops; symlink content is accessible via the real path. |
| Large files (> 10 MB) | Skip, record as ignored | Source files are rarely large; assets over 10 MB are not useful for symbol/import extraction. |
| Mirrored path ambiguity | Record all matches | No heuristic is reliable enough to pick one; surfacing all matches lets the ingestion pipeline or human decide. |
| Binary file line_count | Omit | Line count is meaningless for binary formats. |
| UTF-8 decode errors | Record `parse_error: "encoding"`, continue | Non-UTF-8 files are rare in Python source but should not abort extraction. |
| Test coverage detection | Mirrored path + import resolution (Python only) | Covers the most common conventions without requiring explicit annotations. Explicit `@modok` annotations (Phase 5) will override. |
| Feature/module mapping | Not in extractor | Ownership mapping belongs to the registry (`source_roots`). Extractor produces raw facts only. |
| `.gitignore` auto-update | Never; print a one-time hint | Silently modifying `.gitignore` is surprising. After first extraction, print `Tip: add '.modok/' to .gitignore` if `.modok/` is not already present. |
| `--diff` flag | Deferred | Output is sorted YAML; `git diff .modok/code-map.yml` covers the use case if the file is checked in. Revisit when someone asks. |

---

## Open Questions

None remaining for v1.
