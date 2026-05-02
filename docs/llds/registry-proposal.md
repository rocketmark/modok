# Registry Proposal Engine

## Context and Design Philosophy

The Registry Proposal Engine bootstraps a project's registries (`features.yml`, `modules.yml`, `errors.yml`) by reading the project's existing docs and using the LLM to extract a typed node taxonomy. It runs as part of `modok init --assisted` and produces the registry files that ingestion depends on.

The core discipline mirrors the ingestion pipeline: **mechanical parsing first, LLM enrichment second.** The engine never infers from the whole doc at once — it splits docs mechanically into H2 sections and sends each section independently. This keeps LLM context windows small, output focused, and failure blast radius contained to one section rather than one whole doc.

Two passes:

1. **Per-section enrichment** — each section is sent to the LLM gateway. The LLM extracts typed node candidates (features, modules, error signatures, known issues, failure modes, decisions, observation events) from that section's text only.
2. **Normalisation** — all raw candidates are merged and sent to the LLM in a single normalisation call. The LLM deduplicates, canonicalises phrasing, and enforces type-specific constraints (e.g. ErrorSignature must be a named identifier, not prose).

The output is written directly to `{repo}/registries/`. The user edits the files if the proposals are wrong, then runs `modok ingest`.

The engine does **not** interact with Quine. It is a pre-graph step.

---

## Doc Discovery

The engine discovers files using the same ignore patterns as the ingestion pipeline (SI-DISC-002): `.git/**`, `node_modules/**`, `bin/**`, `obj/**`, `dist/**`, `build/**`, `coverage/**`, `.vs/**`, `.env`, `*.key`, `*.pem`, `*.pfx`.

Eligible file types: `.md`, `.mdx`. YAML files are excluded — they contain structured data, not prose suitable for section extraction.

Files without any H2 headings are skipped (no sections to extract). Files with only frontmatter and no body are also skipped. Both are counted in the proposal summary.

Discovery is recursive from `{repo_root}`. The `registries/` directory itself is excluded — registry files are outputs, not inputs.

A file counts as **processed** if it contributed at least one non-empty section to the enrichment pass. A file counts as **skipped** if it has no H2 headings or all section bodies are empty after stripping whitespace. Both counts appear in the proposal summary.

---

## Mechanical Section Parser

For each discovered file, the parser:

1. Strips YAML frontmatter (content between the opening and closing `---` delimiters).
2. Skips H1 headings (the document title).
3. Splits the remaining body on H2 (`## `) headings. Each H2 and its following content (up to the next H2 or end of file) is one `Section`.
4. Skips sections whose body is empty after stripping whitespace.

Sections whose body is a link list or otherwise content-free are still sent to the LLM — the LLM returns empty results. No content-detection heuristic is applied; the cost of one short call is preferable to a false-positive skip.

Each `Section` carries:
- `heading: str` — the H2 heading text (without `##`)
- `body: str` — the section body (H3+ headings and prose, stripped of the leading `##` line)
- `doc_path: Path` — the source file path (for progress reporting)

The parser is synchronous and has no external dependencies. It must not call the LLM.

---

## Per-Section LLM Enrichment

Each `Section` is sent to the LLM gateway via `modok.llm.gateway.enrich_section(section)` — a new gateway function distinct from `propose_metadata`.

### Node types extracted

| Type | Definition |
|---|---|
| `Feature` | A distinct user-facing capability with its own workflow |
| `Module` | A named software component with a codebase identity (service, library, plugin, executable). Algorithms are not modules. |
| `ErrorSignature` | A named signal emitted when something is wrong or degraded. Must be a named identifier or UI label (e.g. `⚠ No pose`, `GSS_FAILURE`), not a prose description. |
| `KnownIssue` | A documented failure or degraded state with enough detail to diagnose |
| `FailureMode` | An observable failure state at operator level. Root causes (specific hardware parts) are not FailureModes. |
| `Decision` | A design or configuration choice where the doc presents two or more alternatives |
| `ObservationEvent` | A named event code emitted during normal or recovery operation (e.g. `SWEEP_RESUMED`, `TRACKER_CONNECTED`). Must be a named identifier, not prose. |

### Request shape

```python
EnrichSectionRequest:
    section_heading: str
    section_body: str

EnrichSectionResult:
    features: list[str]
    modules: list[str]
    error_signatures: list[str]
    known_issues: list[str]
    failure_modes: list[str]
    decisions: list[str]
    observation_events: list[str]
```

Each value is a list of short strings (canonical name, label, or 3–6 word description). Empty lists are omitted from the LLM response; the gateway fills in empty lists for missing keys before returning.

### LLM call parameters

- Ollama native API (`/api/chat`) with `think: false`, `format: json`, `num_ctx: 8192`.
- System prompt: frozen constant `ENRICH_SECTION_SYSTEM` in `modok.llm.prompts`.
- Timeout per section: `timeout_propose_registry` from `[llm]` in config (default 60s). Separate from `timeout_propose_metadata` — registry sections are larger and less structured.
- One call per section. Sections are processed sequentially (not concurrently) — Ollama is single-threaded on a local machine; concurrent calls queue and timeout rather than parallelise. Concurrency is a future option.
- Before processing begins, print to stderr: `"Found N sections across M docs (~X min at current timeout)"` where X = ⌈N × timeout_propose_registry / 60⌉.
- On LLM failure for a section: emit a warning to stderr, record the section as failed, continue with remaining sections.

---

## Normalisation Pass

After all sections are processed, raw candidates are merged per type into flat lists (string deduplication by exact match). The merged dict is sent to the LLM in a single normalisation call.

### Normalisation rules enforced by the LLM

- **All types**: merge entries describing the same thing into one canonical form; pick the most precise phrasing.
- **ErrorSignature**: keep only named identifiers (SCREAMING_SNAKE_CASE event codes or UI warning labels with symbol). Convert prose forms to their named code equivalents where possible; drop if no named code exists.
- **ObservationEvent**: keep only named event codes or named process identifiers. Drop status dot colors, LED states, prose behavioral descriptions.
- **FailureMode**: merge variants of the same failure state (e.g. "USB bus contention", "USB wedged", "USB glitch" → "USB contention"). Do not merge at a level finer than operator-observable.
- **Decision**: keep only entries where two or more alternatives are present in the doc.

### Normalisation call parameters

- Single Ollama call with `num_ctx: 8192`.
- System prompt: frozen constant `NORMALISE_REGISTRY_SYSTEM` in `modok.llm.prompts`.
- On normalisation failure: skip the normalisation pass, write the raw-merged candidates as-is, emit a warning.

---

## Registry Write

After normalisation, the engine creates `{repo}/registries/` if it does not exist (`mkdir(parents=True, exist_ok=True)`) and writes three YAML files:

### `features.yml`

```yaml
features:
  real-time-camera-tracking:
    name: Real-Time Camera Tracking
    description: Solves tracker position and rotation to stream live spatial data.
  ltc-timecode-sync:
    name: LTC Timecode Sync
    description: Decodes SMPTE LTC from audio inputs to timestamp LiveLink frames.
```

Slugs are derived from the LLM's short string by `slugify()` (see Module Layout). If two entries produce the same slug, the engine keeps the entry with the longer `name` string and emits a stderr warning: `"merged duplicate slug '<slug>': kept '<name>'"`. If the names are clearly unrelated (differ by more than case/whitespace), both are kept: the second gets a `-2` suffix and a stronger warning is emitted: `"slug collision '<slug>': could not merge — review registries/features.yml"`.

### `modules.yml`

```yaml
modules:
  stagehand-client:
    name: Stagehand Client
    description: Windows application that receives SHTP pose data and forwards to LiveLink.
```

### `errors.yml`

```yaml
errors:
  gss-failure:
    normalized_error: GSS_FAILURE
    description: Global scene solver did not converge.
  no-pose:
    normalized_error: "⚠ No pose"
    description: Tracker has lost pose with lighthouses on.
```

Slug is derived from the normalized_error string via `slugify()`. The same collision rules apply as for features and modules.

If a registry file already exists, the engine overwrites it entirely. It does not merge with existing content — the proposal pass is a fresh start.

---

## Proposal Summary

After writing, the engine prints a summary to stdout:

```
Processed 15 sections across 3 docs. 2 sections failed (LLM error).
Wrote registries/features.yml  (8 features)
Wrote registries/modules.yml   (5 modules)
Wrote registries/errors.yml    (16 error signatures)
```

Failed sections are listed by heading on stderr.

---

## Module Layout

```
src/modok/registry/
    __init__.py
    proposal.py        # propose_registries(repo_root, cfg) entry point
    discovery.py       # file discovery, ignore patterns
    parser.py          # Section dataclass, parse_sections(path) -> list[Section]
    writer.py          # write_features_yml, write_modules_yml, write_errors_yml
    slugify.py         # slugify(text) -> str
```

### `slugify(text) -> str`

Deterministic slug derivation applied to all registry keys:

1. Strip leading Unicode non-ASCII symbol characters (`⚠`, `⏸`, `⚡`, etc.) and surrounding whitespace.
2. Replace `(s)` with `s`.
3. Strip remaining parentheses and punctuation characters.
4. Lowercase.
5. Replace runs of non-alphanumeric characters with a single `-`.
6. Strip leading and trailing `-`.
7. Truncate to 40 characters at a word boundary.

Examples: `⚠ N restart(s)` → `n-restarts`, `⏸ Held` → `held`, `GSS_FAILURE` → `gss-failure`, `USB Congestion` → `usb-congestion`.

The LLM calls (`enrich_section`, `normalise_candidates`) are added to `modok.llm.gateway`. The prompts are added to `modok.llm.prompts`. The registry module has no dependency on `modok.quine` or `modok.ingestion`.

---

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|---|---|---|---|
| Section granularity | H2 only | H2 + H3; whole doc | H2 keeps context windows manageable and sections focused. H3 produces too many tiny calls. Whole doc risks 500s and dilutes focus. |
| Sections processed sequentially | Sequential | Concurrent (asyncio.gather) | Ollama is single-threaded on a laptop; concurrent calls queue up and timeout rather than parallelise. Sequential is slower but reliable. |
| Write behaviour | Overwrite entirely | Merge with existing | Merging existing content with new proposals is complex and error-prone. The registry files are meant to be human-edited after the proposal pass; merging would corrupt hand-edits on re-run. Re-run is a deliberate fresh start. |
| Feature slug derivation | Slugify from LLM string | LLM returns slug directly | Asking the LLM to return slugs increases prompt complexity. Slugifying a short descriptive string is deterministic and trivially correct. |
| Modules in registry | Included in proposal | Features only | Modules are extracted anyway; discarding them wastes signal. The module registry is needed for LLD frontmatter validation. |
| Normalisation as second LLM pass | Separate pass | In-prompt rules only; post-processing code | Normalisation rules (e.g. "USB bus contention + USB wedged → USB contention") require semantic understanding. Code-based deduplication only catches exact strings. A second LLM pass is the right tool. |

---

## Open Questions & Future Decisions

1. **Re-run behaviour** — currently overwrites entirely. Should a future `--merge` flag preserve hand-edits and only add new entries? Defer until someone needs it.
2. **Module-to-feature linkage** — the proposal engine extracts features and modules separately. It does not attempt to associate which modules belong to which feature. That association would require a third LLM pass or manual editing. Defer.
3. **Context window for large sections** — `num_ctx: 8192` handles most sections. Very large sections (e.g. a long Troubleshooting section) may exceed this. A section-splitting fallback (by character count) is not implemented in v1.
4. **Confidence scores** — the normalisation pass could return a confidence score per entry to flag low-confidence proposals. Not in v1; the user edits directly.
5. **Concurrent section processing** — sequential is intentional for v1. If Ollama gains multi-request support or a remote backend is used, bounded concurrency (`asyncio.gather` with semaphore) is the natural upgrade path.
