# Import Arrow

## Context and Design Philosophy

`modok import-arrow` is a structured registry bootstrap tool for projects that maintain arrow docs — a `docs/arrows/index.yaml` index plus per-arrow `.md` files with `### Code` and `### Key Components` sections. It produces `features.yml` and `modules.yml` directly from those structured sources, validated against the code map.

The guiding principle: mechanical extraction from structured sources is preferred over LLM-from-docs whenever the structured source exists. Arrow docs are curated by the developer as part of the LID workflow; their `### Code` sections are already the authoritative list of files per feature, and `### Key Components` is the authoritative list of modules. The code map is the validator.

LLM is used in two narrow, bounded passes only:

1. **Name/description generation** — for modules where the filename slug is not self-evident, a batched call generates `name` and `description`. Modules where title-casing the slug is obviously correct skip the call.
2. **Dedup resolution** — after set-math duplicate detection, a per-pair call first determines whether two candidates represent the same concept (not just the same files), then picks the better label if so. If they are different concepts sharing files, the pair is flagged as a `CONFLICT` for human review.

No LLM call writes to `features.yml` or `modules.yml` directly. All LLM output is reviewed by the structured extraction logic before being written.

---

## Command Interface

```bash
modok import-arrow --project <slug> [--repo <path>] [--dry-run] [--no-llm]
```

- `--project` — required. Used to look up repo path from config and to namespace the output.
- `--repo` — optional override for the repo root path.
- `--dry-run` — print the proposed `features.yml` and `modules.yml` to stdout without writing files. Does not run the LLM passes.
- `--no-llm` — skip both LLM passes. Names default to title-cased slug; descriptions default to empty string. Useful for CI or when a human will edit the output.

On success, prints a summary to stdout:

```
Imported 5 features, 12 modules → registries/features.yml, registries/modules.yml
  2 modules used LLM name generation
  1 dedup pair resolved (kept: fbx-writer)
  0 conflicts flagged
```

On failure (missing index.yaml, missing code map, CONFLICT pairs), exits non-zero with a clear message. `CONFLICT` pairs are printed to stdout and the command exits non-zero; partial output is not written.

If `index.yaml` contains no arrows, the command exits zero and prints `No arrows found in index.yaml — nothing to import.` No files are written, and existing registry files are not clobbered.

On re-runs, both `features.yml` and `modules.yml` are always regenerated together. Partial regeneration (one file but not the other) is not supported — the two files must be consistent with each other.

---

## Inputs

### Arrow index (`docs/arrows/index.yaml`)

Required. Parsed with `yaml.safe_load`. Expected schema per entry:

```yaml
- id: freed-output
  description: "..."
  arrow_doc: docs/arrows/freed-output.md
  specs: docs/specs/freed-output-specs.md
  tests:
    - client/tests/test_freed.py
  # other fields ignored
```

Missing `id`, `description`, `arrow_doc`, or `specs` on any entry is a hard error.

### Arrow docs (`docs/arrows/<id>.md`)

Each arrow doc is parsed for two sections:

**`### Code`** — source files for the feature. Extraction rule:
- For each `-` bullet line, split on the em-dash separator, take only the left side. Separator is normalised: accept `—` (U+2014), `–` (U+2013), and ` - ` (spaced hyphen). Regex: `re.split(r'\s*[—–]|\s+-\s+', line, maxsplit=1)`.
- Extract all backtick-quoted strings from the left side.
- Keep only strings containing `/` or `.` and not containing `(` (file paths, not symbol names).
- Multiple paths on one line (comma-separated backtick items) are all captured.

**`### Key Components`** — module definitions. Extraction rule:
- Numbered list items: `` `ClassName` — description ``
- Extract primary class/function name (first backtick item per line).
- Extract description (text after `—` on the same line).
- Match class name against the code map's symbol table to find the owning source file.
- If the same class name appears in multiple arrow docs' `### Key Components` sections, emit `WARN: ambiguous Key Components symbol <name> — appears in multiple arrows, skipping overlay` and do not apply the description. File-per-module default still applies.

If `### Code` is absent from an arrow doc, the feature gets `source_files: []` and a warning is emitted.
If `### Key Components` is absent, module extraction falls back to file-per-module from the feature's `source_files` list.

### Code map (`.modok/code-map.yml`)

Required. Loaded with `yaml.safe_load`. Used for:
1. Validating that each `source_files` path exists in the code map and is not `role: ignored`.
2. Symbol lookup for `### Key Components` class-to-file matching.
3. Supplying `language` and `primary_class` for each module entry.

If the code map does not exist, the command prints a warning and runs `extract-code-map` automatically before proceeding (consistent with CM-AUTO-001 behaviour).

---

## Feature Extraction

One feature entry per arrow in `index.yaml`. Dict-keyed by slug.

```yaml
features:
  freed-output:
    name: Freed Output          # title-case of slug
    description: "..."          # from index.yaml description field
    source_files: [...]         # from ### Code section
    test_files: [...]           # from index.yaml tests, filtered and cleaned
    specs: docs/specs/...       # from index.yaml specs field
    modules: []                 # populated after module extraction
```

**`test_files` cleaning:**
- Drop entries starting with `spec/` (TLA+ specs, not pytest files).
- Strip trailing annotations: split on `(`, take the left side, strip whitespace.
- Drop entries that do not look like file paths (no `/` or `.`).

**`source_files` validation:**
- Each path is looked up in the code map.
- Not found → `WARN: [slug] source file not in code map: <path>` (continues, does not block).
- Found with `role: ignored` → `WARN: [slug] source file is ignored in code map: <path>`.

---

## Module Extraction

Two-pass process: file-per-module default, then `### Key Components` overlay.

### Pass 1 — file-per-module (mechanical)

For each path in each feature's `source_files`:
- Skip non-source files (language `unknown`, role `config`, `docs`, `generated`, `ignored`).
- **Python files** → file-per-module. Slug: filename stem, underscores replaced with hyphens (`shtp_receiver.py` → `shtp-receiver`). `source_files: [path]`, `source_roots: []`.
- **C/cpp directory entries** (path ends with `/` or has no extension and is a directory in the code map) → single module with `source_roots: [path]`, `source_files: []`. Slug: directory name, underscores/slashes replaced with hyphens. The heuristic: if the majority of files under the path have `language: cpp`, treat as a directory-level module rather than file-per-module.
- `language`: from code map.
- `primary_class`: first class or function symbol in the code map symbol list for this file; empty string if none or if directory-level.
- `name`: title-case of slug (preliminary; may be overwritten by LLM pass).
- `description`: `""` (preliminary; may be overwritten by `### Key Components` or LLM pass).

### Pass 2 — Key Components overlay

For each numbered item in `### Key Components`:
- Extract primary class/function name and description.
- Look up the class name in the code map symbol table (all files).
- If found in exactly one file: update that file's module entry with the description from the arrow doc.
- If found in multiple files: emit `WARN: ambiguous symbol <name>, skipping Key Components match`.
- If not found: emit `WARN: symbol <name> not in code map, skipping Key Components match`.

### Pass 3 — Deduplication

Build an inverted index: `source_file → [module slugs]`.

**Exact duplicates** (identical `source_files` frozensets, two or more slugs):
- Flag pair for LLM dedup resolution (see below).

**Subset pairs** (A's file set is a strict subset of B's):
- Emit `WARN: [A] source_files is a strict subset of [B] — may be redundant` and continue; do not auto-merge. A strict subset might be a legitimate sub-module; human decides.

**File claimed by multiple non-duplicate modules:**
- Emit `WARN: <path> claimed by multiple modules: [slug-A, slug-B]` and continue. A module whose source files are claimed by multiple features is intentional — it becomes a hub node in the graph. The same module slug appears in both features' `modules` lists; this is correct behaviour, not an error.

---

## LLM Passes

Both passes are skipped when `--no-llm` is set or `--dry-run` is set.

### Pass 1 — Name and description generation (batched)

Candidates: modules where `description` is still `""` after the Key Components overlay, or where the slug is not obviously self-describing (heuristic: slug contains an acronym — all-caps component — e.g. `shtp`, `ltc`, `smll`).

Modules where title-casing the slug is clearly sufficient (e.g. `fbx-writer`, `event-log`, `paths`) are excluded from the batch.

Single LLM call with all candidates. Input per module: slug, primary_class, source_file path, language. Output per module: `name` (short, user-facing), `description` (one sentence).

### Pass 2 — Dedup resolution (one call per flagged pair)

Input: slug-A, name-A, description-A, slug-B, name-B, description-B, shared source files.

Two-question structure:
1. "Are these the same concept, or different conceptual views of the same code?"
   - If different → emit `CONFLICT: [slug-A] vs [slug-B] — same files, different concepts. Human review required.` Command exits non-zero. No output written.
2. If same concept: "Which label is more user-facing?" → `keep: slug`, `description: str`.

---

## Output

### `registries/features.yml`

```yaml
features:
  pi-agent:
    name: Pi Agent
    description: "Pi-side C agent — SHTP encoding, recovery state machine, ..."
    source_files:
      - agent/src/recovery.c
      - agent/src/recovery.h
      - agent/src/shtp.c
      - agent/src/main.c
    test_files:
      - agent/tests/test_scenarios.py
    specs: docs/specs/pi-agent-specs.md
    modules:
      - pi-recovery
      - pi-shtp
      - pi-main
```

`modules` list is populated from the slugs of all module entries whose `source_files` are a subset of the feature's `source_files`.

### `registries/modules.yml`

```yaml
modules:
  fbx-writer:
    name: FBX Writer
    description: Pure Python FBX 7.4 binary encoder for post-production export
    source_files:
      - client/stagehand_client/fbx_writer.py
    source_roots: []
    primary_class: write_fbx
    language: python
```

### Output directory

Both files are written to `{repo_root}/registries/`. The directory is created if it does not exist. Existing files are overwritten without prompting (the user ran `import-arrow`, which implies intent to regenerate).

---

## Non-Arrow Modules

Source files that appear in the code map but are not claimed by any arrow's `source_files` are not automatically added to `modules.yml`. They are surfaced in a summary line at the end of the run:

```
12 source files in code map not claimed by any arrow:
  client/stagehand_client/math_utils.py
  client/stagehand_client/log_format.py
  ...
```

These are candidates for a manual `modules.yml` addition or a future `--include-unclaimed` flag.

---

## Decisions & Alternatives

| Decision | Choice | Rationale |
|---|---|---|
| Arrow index as single source of feature list | `index.yaml` only | Structured, machine-readable, already maintained by LID workflow. Avoids parsing prose. |
| Source files from `### Code` section | Left side of `—` only | Right side is symbols, not paths. Em-dash is a consistent separator across all arrow docs in stagehand. |
| Module slug from filename stem | Always | Deterministic, no LLM needed. Human-readable name is a separate field. |
| `### Key Components` for descriptions | Overlay, not primary | File-per-module is the mechanical default; Key Components corrects names/descriptions where the arrow doc has already made the judgment. |
| Dedup via set math | Exact frozenset equality | Sufficient for the case this is designed to handle (same file, two names from two sources). Subset detection is advisory only. |
| CONFLICT exits non-zero | Hard stop | A CONFLICT means the module graph has an integrity problem. Silent continuation would produce a broken registry. |
| Non-arrow files not auto-added | Surfaced as warning only | Utility files (math_utils, log_format) may not need a module entry at all. Auto-adding them creates noise. `--include-unclaimed` deferred. |
| `--no-llm` flag | Skip both LLM passes | Allows CI use and human-edit workflows where descriptions will be curated manually. |
| Overwrite without prompt | Always regenerate both files | `import-arrow` is a regeneration command. Both files must be consistent; partial regeneration is not supported. |
| C directory modules | Single module with `source_roots` | C files compile as a unit and share headers; file-per-module would fragment a logical whole. Python stays file-per-module. |
| Subset dedup | Warn-only, no auto-merge | A strict subset might be a legitimate sub-module. Auto-merging would silently destroy valid structure. |
| Shared-file modules in multiple features | Intentional, both `modules` lists get the slug | Creates a hub node in the graph with edges to both features. Correct behaviour, not an error. |
| Em-dash separator normalisation | Accept U+2014, U+2013, and spaced hyphen | Editor variance is real; regex normalisation makes extraction robust without manual doc fixes. |
| Key Components class name collision | Skip overlay, emit warning | Ambiguous which instance to apply; file-per-module default stands. Human edits description. |
| Empty index.yaml | Exit zero, write nothing | Avoid clobbering existing registry files on a misconfigured run. |

---

## Open Questions

None remaining for v1.
