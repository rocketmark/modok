# Import Arrow Specs

Specs for `modok.import_arrow` — the structured registry bootstrap command that reads arrow docs and writes `features.yml` and `modules.yml`.

LLD: `docs/llds/import-arrow.md`

---

## Test Level Convention

- **[U]** — Unit test with mocked dependencies.
- **[I]** — Integration test against a real (tmp_path) directory tree.

---

## Command Interface

- [ ] **IA-CMD-001** [U]: The system shall expose a CLI command `modok import-arrow` accepting `--project <slug>`, optional `--repo <path>`, optional `--dry-run`, and optional `--no-llm`.
- [ ] **IA-CMD-002** [U]: When `--repo` is omitted, the system shall use the project's configured repo path from `~/.modok/config.toml`. When the project slug is not found in config, the command shall exit non-zero with a clear error.
- [ ] **IA-CMD-003** [U]: When `docs/arrows/index.yaml` does not exist in the repo root, the command shall exit non-zero with the message `index.yaml not found at <path>`.
- [ ] **IA-CMD-004** [U]: When `index.yaml` contains no arrows, the command shall exit zero and print `No arrows found in index.yaml — nothing to import.` No files shall be written and no existing registry files shall be modified.
- [ ] **IA-CMD-005** [U]: On success, the command shall print a summary to stdout: `Imported N features, M modules → registries/features.yml, registries/modules.yml` followed by lines for LLM usage count, dedup pairs resolved, and conflicts flagged.
- [ ] **IA-CMD-006** [U]: When `--dry-run` is set, the command shall print the proposed `features.yml` and `modules.yml` content to stdout and exit zero without writing any files. LLM passes shall not run.
- [ ] **IA-CMD-007** [U]: When `--no-llm` is set, both LLM passes shall be skipped. Module names shall default to title-cased slug and descriptions shall default to empty string.
- [ ] **IA-CMD-008** [U]: When one or more CONFLICT pairs are detected, the command shall print each conflict to stdout, exit non-zero, and write no output files.

---

## Feature Extraction

- [ ] **IA-FEAT-001** [U]: For each arrow entry in `index.yaml`, the system shall produce one feature entry keyed by the arrow's `id` field.
- [ ] **IA-FEAT-002** [U]: The feature `name` shall be the title-cased form of the slug (`freed-output` → `Freed Output`).
- [ ] **IA-FEAT-003** [U]: The feature `description` shall be the `description` field from `index.yaml` verbatim.
- [ ] **IA-FEAT-004** [U]: The feature `specs` field shall be the `specs` field from `index.yaml` verbatim.
- [ ] **IA-FEAT-005** [U]: The feature `test_files` shall be derived from the `tests` list in `index.yaml` with: entries starting with `spec/` dropped; trailing annotations (text after `(`) stripped; entries not containing `/` or `.` dropped.
- [ ] **IA-FEAT-006** [U]: The feature `source_files` shall be extracted from the Code section of the arrow doc. The Code section is detected by either a `### Code` H3 header or a `**Code:**` bold-with-colon label (both formats are accepted). For each `-` bullet line: the line shall be split on the first em-dash separator (normalised to accept U+2014, U+2013, and spaced hyphen); all backtick-quoted strings on the left side of the separator shall be extracted; strings containing `/` or `.` and not containing `(` shall be kept as file paths.
- [ ] **IA-FEAT-007** [U]: When an arrow doc has no Code section (neither `### Code` nor `**Code:**`), the feature shall be produced with `source_files: []` and a warning shall be printed to stderr.
- [ ] **IA-FEAT-008** [U]: Each path in `source_files` shall be validated against the code map. A path absent from the code map shall produce a warning to stderr and shall still be included in the output. A path present in the code map with `role: ignored` shall produce a separate warning.
- [x] **IA-FEAT-009** [U]: An arrow index entry missing any of `id`, `description`, `arrow_doc`, or `specs` — whether the key is absent or present with a falsy/null value — shall cause the command to exit non-zero with a clear error identifying the missing field and the entry.
- [x] **IA-FEAT-010** [U]: If an entry in `index.yaml`'s `tests` list is not a string (e.g. an unquoted list item containing a colon, which YAML parses as a single-key mapping rather than a scalar), the system shall skip that entry with a warning to stderr rather than raising.

---

## Module Extraction — Pass 1 (Mechanical)

- [ ] **IA-MOD-001** [U]: For each Python source file in a feature's `source_files` (language `python`, role `source` in the code map), the system shall produce one module entry. The slug shall be the filename stem with underscores replaced by hyphens.
- [ ] **IA-MOD-002** [U]: After collecting all source files across all features, the system shall group `language: cpp` source files by their parent directory. Any parent directory containing two or more cpp source files shall produce one directory-level module entry with `source_roots: [parent_dir]` and `source_files: []`. The slug shall be the directory path with path separators and underscores replaced by hyphens and leading/trailing hyphens stripped. Individual C/cpp files belonging to a grouped directory shall not produce separate module entries.
- [ ] **IA-MOD-003** [U]: Each module entry shall contain `language` (from the code map), `primary_class` (first class or function symbol for the file; empty string if none or if directory-level), `name` (title-cased slug, preliminary), and `description` (empty string, preliminary).
- [ ] **IA-MOD-003b** [U]: When a source file in a feature's `source_files` is absent from the code map, the system shall still produce a module entry for it with `language: unknown`, `primary_class: ""`, and a warning emitted to stderr. The module is included with incomplete metadata rather than skipped.
- [ ] **IA-MOD-004** [U]: Files with role `config`, `docs`, `generated`, or `ignored` in the code map shall not produce module entries.
- [ ] **IA-MOD-005** [U]: A module slug that would be generated from multiple source files (same stem, different directories) shall produce one entry per file with the slug formatted as `{parent_dir_name}-{stem}` using only one level of directory prefix (e.g. `src-engine` and `lib-engine`). Slashes and underscores in the prefix are replaced with hyphens.

---

## Module Extraction — Pass 2 (Key Components Overlay)

- [ ] **IA-OVER-001** [U]: For each numbered item in a Key Components section (detected by either `### Key Components` H3 header or `**Key Components:**` bold-with-colon label), the system shall extract the primary class or function name (first backtick-quoted string, with trailing `()` stripped) and the description (text after the em-dash separator).
- [ ] **IA-OVER-002** [U]: The system shall look up each extracted class or function name in the code map's symbol table. When found in exactly one file, the corresponding module entry's `description` shall be updated with the Key Components description.
- [ ] **IA-OVER-003** [U]: When a class or function name from a Key Components section is found in multiple files in the code map, the system shall emit a warning to stderr and skip the overlay for that name.
- [ ] **IA-OVER-004** [U]: When a class or function name from a Key Components section appears in multiple arrow docs' sections, the system shall emit a warning to stderr and skip the overlay for that name.
- [ ] **IA-OVER-005** [U]: When a class or function name from a Key Components section is not found in the code map symbol table, the system shall emit a warning to stderr and skip the overlay for that name.
- [ ] **IA-OVER-006** [U]: When an arrow doc has no Key Components section (neither `### Key Components` nor `**Key Components:**`), module extraction for that arrow's files shall use Pass 1 results only. No warning is emitted for a missing Key Components section.

---

## Deduplication

- [ ] **IA-DEDUP-001** [U]: The system shall build an inverted index of `source_file → [module slugs]` after Pass 1 and Pass 2.
- [ ] **IA-DEDUP-002** [U]: When two module candidates have identical `source_files` frozensets, the system shall flag them as a duplicate pair for LLM resolution (or, with `--no-llm`, emit a warning and keep both).
- [ ] **IA-DEDUP-003** [U]: When module A's `source_files` is a strict subset of module B's `source_files`, the system shall emit a warning to stderr and continue without merging.
- [ ] **IA-DEDUP-004** [U]: When a source file is claimed by multiple non-duplicate module candidates, the system shall emit a warning to stderr and continue. This is permitted when the modules belong to different features.

---

## LLM Passes

- [ ] **IA-LLM-001** [U]: The name/description LLM pass shall be skipped for modules where the slug is self-evidently title-caseable (no acronym components — all-caps segments of length ≥ 2) and a Key Components description was successfully applied.
- [ ] **IA-LLM-002** [U]: The name/description LLM pass shall send all qualifying modules in a single batched call. The input per module shall include: slug, primary_class, source_file path, language.
- [ ] **IA-LLM-003** [U]: The dedup LLM pass shall send one call per flagged duplicate pair. The input shall include: slug-A, name-A, description-A, slug-B, name-B, description-B, shared source files.
- [ ] **IA-LLM-004** [U]: When the dedup LLM pass determines two candidates are the same concept, the system shall retain the slug identified as more user-facing and discard the other.
- [ ] **IA-LLM-005** [U]: When the dedup LLM pass determines two candidates are different concepts sharing the same files, the system shall emit a `CONFLICT` record, exit non-zero, and write no output files.
- [ ] **IA-LLM-006** [U]: When `--no-llm` is set, duplicate pairs shall produce a warning instead of a LLM call, and both entries shall be retained in the output.
- [ ] **IA-LLM-007** [U]: When the name/description LLM call (Pass 1) fails (timeout, parse error, or malformed response), the system shall fall back to title-cased slug defaults for all affected modules, emit a warning to stderr, and continue. The command shall not exit non-zero for a Pass 1 failure.
- [ ] **IA-LLM-008** [U]: When the dedup LLM call (Pass 2) fails for a pair, the system shall treat the pair as a CONFLICT, exit non-zero, and write no output files.

---

## Feature → Module Wiring

- [ ] **IA-WIRE-001** [U]: After all module entries are finalised, each feature's `modules` list shall be populated with the slugs of all file-level modules whose `source_files` are a non-empty subset of that feature's `source_files`.
- [ ] **IA-WIRE-002** [U]: A module whose source files are claimed by multiple features shall appear in all of those features' `modules` lists. This is intentional and shall not produce a warning.
- [ ] **IA-WIRE-003** [U]: Directory-level modules (those with `source_roots` and `source_files: []`) shall be wired to a feature when any path in the feature's `source_files` starts with the module's `source_roots` prefix. Set-inclusion logic shall not be used for directory-level modules.

---

## Output

- [ ] **IA-OUT-001** [I]: The system shall write `registries/features.yml` and `registries/modules.yml` to the repo root. The `registries/` directory shall be created if it does not exist.
- [ ] **IA-OUT-002** [U]: Both output files shall be valid YAML parseable by `yaml.safe_load` without error.
- [ ] **IA-OUT-003** [U]: `features.yml` shall be keyed by feature slug and contain `name`, `description`, `source_files`, `test_files`, `specs`, and `modules` fields for each entry.
- [ ] **IA-OUT-004** [U]: `modules.yml` shall be keyed by module slug and contain `name`, `description`, `source_files`, `source_roots`, `primary_class`, and `language` fields for each entry.
- [ ] **IA-OUT-005** [U]: Both output files shall always be regenerated together. Partial regeneration (one file without the other) shall not occur.
- [ ] **IA-OUT-006** [I]: When `--dry-run` is set, neither output file shall be created or modified.

---

## Non-Arrow Modules

- [ ] **IA-UNCLAIMED-001** [I]: After writing output files, the system shall print to stdout the list of source files present in the code map that are not claimed by any feature's `source_files`. The count and paths shall be printed. No module entries shall be created for these files automatically.
