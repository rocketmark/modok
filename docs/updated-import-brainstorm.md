# Updated Registry Import — Brainstorm

Captures the approach decided in the 2026-05-03 session. Supersedes the ad-hoc normalise/proposal pipeline for initial registry population.

---

## Core Insight

The module is the primary search node in the knowledge graph. A user queries by module → graph traverses to source files, test files, docs, specs, and features. Features are a classification/grouping layer, not a search entry point. The code map validates that claimed file paths actually exist.

**Hierarchy:**
```
Feature (arrow-level, broad)
  └── Module (file-level, searchable hub)
        └── Source files (validated against code map)
        └── Test files
        └── Docs (reach via ingestion claims)
        └── Specs (reach via feature edge)
```

---

## Features

### Source

- `docs/arrows/index.yaml` — machine-readable: slug (`id`), description, test file list, specs path, arrow doc path
- `docs/arrows/<id>.md` `### Code` section — source file list (one file path per bullet before the `—` separator)

### Schema (extended)

```yaml
features:
  freed-output:
    name: Freed Output
    description: FreeD D1 UDP output mode — packet encoding, coordinate transform, feature-flag gating, configuration
    source_files:
      - client/stagehand_client/freed_sender.py
      - client/stagehand_client/main.py
      - client/stagehand_client/config.py
    test_files:
      - client/tests/test_freed.py
      - client/tests/test_output_consistency.py
      - client/tests/test_feature_flags.py
    specs: docs/specs/freed-output-specs.md
    modules: []   # populated after modules pass
```

### Extraction rules (mechanical)

1. Read `index.yaml` → one entry per arrow
2. `slug` = `id` field
3. `name` = title-case of slug (`freed-output` → `Freed Output`)
4. `description` = `description` field verbatim
5. `specs` = `specs` field verbatim
6. `test_files` = `tests` list, drop entries starting with `spec/`, strip trailing annotations like `(not yet written)`
7. `source_files` = from `### Code` section of `arrow_doc`:
   - For each `-` bullet line, split on `—` (em-dash), take only the left side
   - Extract all backtick-quoted strings from the left side
   - Keep only strings containing `/` or `.` and no `(` (file paths, not symbol names)

### What features do NOT cover

- User-vocabulary search (too coarse, too architectural)
- Module-level granularity (that's modules)
- Features are for: doc classification, LLD/spec anchoring, grouping modules

---

## Modules

### Source

- **Code map** (`code-map.yml`) — file paths, language, role, symbols (class/function names)
- **Arrow docs** `### Key Components` section — module names, descriptions, grouping decisions

### Schema (extended from current)

```yaml
modules:
  fbx-writer:
    name: FBX Writer
    description: Pure Python FBX 7.4 binary encoder for post-production export
    source_files:
      - client/stagehand_client/fbx_writer.py
    source_roots: []          # for directory-level modules (C code, etc.)
    primary_class: write_fbx  # top symbol from code map
    language: python
```

### Extraction rules

**Mechanical (from code map):**
1. For each Python source file in `client/stagehand_client/` (role=source, language=python):
   - `slug` = filename stem, underscores→hyphens (`fbx_writer.py` → `fbx-writer`)
   - `source_files` = [the path]
   - `primary_class` = first class or function symbol in the file's symbol list
   - `language` = from code map
2. For C source directories (e.g. `agent/src/`):
   - `slug` = directory name (`agent-src` or a named equivalent)
   - `source_roots` = [the directory path]
   - `language` = cpp/c

**Needs judgment (one pass, human or LLM):**
- `name` — title-case of slug is a default, but user-facing name may differ (`fbx-writer` → "FBX Writer" is fine; `shtp-receiver` → "SHTP Receiver" needs domain knowledge)
- `description` — derivable from arrow doc `### Key Components` entry for this file; otherwise from primary class docstring; otherwise LLM single-call
- **Grouping** — some logical modules span multiple files (e.g. `shtp_recorder.py` + `shtpcap_reader.py`). Arrow doc `### Key Components` is authoritative for grouping.

### Grouping from arrow docs

Each `### Key Components` section describes logical modules. Parse numbered list items:

```
1. `ShtpRecorder` — thread-safe SHTPCAP writer; ...
2. `LtcTimecodeService` — background audio decode thread; ...
```

Match primary class name against code map symbols to find the owning file. This resolves grouping mechanically when the class name is unique.

---

## Deduplication Pass

After generating module candidates, detect redundant nodes before writing.

### Detection (mechanical — set math)

```python
# Build inverted index
file_to_modules = defaultdict(list)
for slug, entry in modules.items():
    for f in entry.get('source_files', []):
        file_to_modules[f].append(slug)

# Exact duplicates
file_sets = {slug: frozenset(entry.get('source_files', [])) for slug, entry in modules.items()}
for a, b in combinations(file_sets, 2):
    if file_sets[a] == file_sets[b]:
        flag(DUPLICATE, a, b)
    elif file_sets[a] < file_sets[b]:
        flag(SUBSET, a, b)   # a might be redundant inside b
```

### Merge (one LLM call per flagged pair)

Input: two slugs + their descriptions. Question: "which label is more user-facing?" Output: keep one slug, discard the other. Low stakes, single call per pair.

---

## Code Map Wiring

### Validation at write time

Before writing `modules.yml`, cross-check every `source_files` entry against the code map:
- File exists in code map → ✓
- File in code map but `role: ignored` → warn (probably intentional)
- File not in code map → error (path is wrong or file was deleted)

```python
code_map_paths = {e['path'] for e in code_map['files'] if e.get('role') != 'ignored'}
for slug, entry in modules.items():
    for f in entry.get('source_files', []):
        if f not in code_map_paths:
            print(f"WARN [{slug}]: {f} not in code map")
```

### Validation at ingest time

When a doc claims a source file (via `source_files` frontmatter or parsed reference):
1. Check file exists in code map
2. Check file is claimed by at least one module → surface module context
3. If file has no module claim → warn "unclaimed file"

This is the CM-AUTO cascade (ingestion-pipeline segment, currently deferred).

---

## Registry class changes needed

Already implemented in `src/modok/ingestion/registry.py`:
- `source_files_for_feature(slug)` ✓
- `test_files_for_feature(slug)` ✓
- `specs_for_feature(slug)` ✓
- `features_for_source_file(repo_path)` ✓
- `modules_covering_path` extended to support `source_files` alongside `source_roots` ✓

Still needed:
- `source_files_for_module(slug)` — mirrors the feature accessor
- `modules_for_source_file(repo_path)` — complement to `modules_covering_path`, returns exact-match slugs
- `primary_class_for_module(slug)` — for graph node labelling

---

## What needs LID

This touches three existing segments and introduces a new one:

| Segment | Impact |
|---|---|
| Registry schema | Schema extension: `source_files`, `test_files`, `specs`, `primary_class` fields. Needs spec updates (RP- prefix). |
| Ingestion pipeline | Code map validation at ingest time (CM-AUTO-001–004 cascade, currently deferred). |
| Code map extractor | Already implemented. Pending cascade to ingestion-pipeline segment. |
| **New: `modok import-arrow`** | New CLI command: reads `index.yaml` + arrow docs + code map → writes `features.yml` + `modules.yml`. Mechanical extraction + dedup pass. |

The new `modok import-arrow` command is the cleanest scope for LID. The registry schema extension and ingestion validation are cascades from that.

---

## Open Questions

1. **Grouping authority** — if `### Key Components` disagrees with the file-per-module default, which wins? Suggest: arrow doc wins; code map is the validator, not the arbiter of grouping.
2. **Shared files** — files appearing in multiple features (e.g. `main.py` in `freed-output` and `client-signal-and-output`) are intentional. Same file in multiple modules would be unusual — flag but don't block.
3. **`alembic_writer.py`** — declared in arrow doc but doesn't exist in code map. Policy: include in registry (it's planned), code map cross-check emits a warning, not an error.
4. **Non-arrow modules** — utility files not owned by any arrow (`math_utils.py`, `log_format.py`, `paths.py`). Code map surfaces them; they get a module entry with no feature parent. Is that OK or should they be attached to the nearest arrow?
