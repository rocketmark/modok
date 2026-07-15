# Adding a Project to MODOK

Assumes [`docs/setup.md`](setup.md) is done: Quine is running, a local LLM backend is up, and `~/.modok/config.toml` has `[quine]` and `[llm]` sections. This doc adds one project repo and walks it through registry bootstrap and first ingestion.

Repeat this whole doc once per project you want MODOK to index.

---

## Step 1 — Choose a project repo to index

MODOK needs the project repo on disk to validate file references in registries and docs. The repo does not need to be built — just cloned.

**If you are following this guide for the first time**, use the `modok` repo itself as your sample project — it is already on disk from `docs/setup.md`. It is fully self-contained (Python source, docs, tests) and gives you a real codebase to index without touching anything you care about.

**To index your own project**, clone it now:

```bash
git clone https://github.com/yourorg/yourproject ~/github/yourproject
```

The rest of this guide uses `modok` as the sample project slug and `~/github/modok` as the repo path. Substitute your own values if you are indexing a different repo.

---

## Step 2 — Initialize the project

```bash
modok init --project modok --repo ~/github/modok
```

This:
- Registers the project in `~/.modok/config.toml` if not already present (adds a `[[projects]]` entry)
- Installs a git post-commit hook in the repo that runs ingestion automatically on commits touching docs, registries, or tickets
- Creates empty stub files for `registries/features.yml`, `registries/modules.yml`, `registries/errors.yml`, and `registries/doc-types.yml` if they don't already exist

---

## Step 3 — Generate the code map

The code map is a YAML snapshot of every file in the repo — language, role, symbols, and SHA256. It is the foundation for registry bootstrap and file validation.

```bash
modok extract-code-map --project modok --repo ~/github/modok
```

Output is written to `<repo>/.modok/code-map.yml` (gitignored). Re-run this any time the repo's file structure changes significantly.

```
Extracted code map: 320 files (71 source, 68 test, 84 config, 97 docs) → /path/to/modok/.modok/code-map.yml
```

---

## Step 4 — Bootstrap registries from arrow docs

If the project has arrow docs (`docs/arrows/index.yaml`), use `import-arrow` to generate `registries/features.yml` and `registries/modules.yml` directly from them. This replaces the empty stubs created by `modok init`.

```bash
modok import-arrow --project modok --repo ~/github/modok
```

This reads each arrow doc's `### Code` and `### Key Components` sections, validates all file paths against the code map, and writes both registry files. Add `--no-llm` to skip name/description generation (useful for CI or first runs):

```bash
modok import-arrow --project modok --repo ~/github/modok --no-llm
```

Add `--dry-run` to preview the proposed registries without writing anything.

**If the project does not have arrow docs** (including the `modok` sample project), you have two options:

- Edit `registries/features.yml` and `registries/modules.yml` manually using the stubs created by `modok init`.
- Use the [LID project](https://github.com/marks/lid) to generate arrow docs from your codebase, then run `import-arrow`. LID produces structured design docs that `import-arrow` can consume directly.

---

## Step 5 — Run first ingestion

Run the three ingestion commands **in this order**. Each one builds on the previous:

1. `modok ingest` — reads docs and writes the static knowledge graph (Feature, Module, File, DocSection nodes and their edges). Must run first because the other two commands depend on File nodes being present.
2. `modok ingest-git` — walks git log and writes Commit nodes with `TOUCHES` edges to File nodes. Requires File nodes from step 1.
3. `modok ingest-elements` — extracts code identifiers from source files and writes `registries/elements.yml`. Does not touch the graph; enriches the registry used by `modok retrieve` at query time, and feeds mechanical feature anchor linking (`docs/llds/standing-queries.md § Mechanical Anchor Linking`).

**Ingest docs:**

```bash
modok ingest --project modok
```

Discovers docs using three-tier discovery: Tier 1 walks `docs/arrows/index.yaml` and ingests each registered LLD, spec, and arrow doc with metadata derived from the registries; Tier 2 scans remaining `docs/**/*.md` files and infers `doc_type` and `feature` from path conventions; Tier 3 ingests anything that doesn't resolve to a known feature slug as `doc_type: unregistered`. You should see a structured report:

```
Ingestion complete
  Docs processed:  24
  Nodes written:   312
  Edges written:   487
  Warnings:        0
  Errors:          0
  LLM proposals:   0
  Pending items:   0
  Files ignored:   5
  Unregistered:    3
  Duration:        1.4s
```

Unregistered docs are listed below the summary — they are a discovery signal, not errors. A doc showing up unregistered usually means it belongs in `docs/arrows/index.yaml` or its filename doesn't match a known feature slug yet.

**Ingest git history:**

```bash
modok ingest-git --project modok
```

Imports commits that touch registered source files and docs as `Commit` nodes in the graph, with `TOUCHES` edges to the relevant `File` nodes. By default imports the last 6 months / 500 commits. Use `--full` for an initial bootstrap of the full history, or `--since 2025-01-01` to import from a specific date. Subsequent runs are incremental — only commits since the last run are imported.

**Extract module code identifiers:**

```bash
modok ingest-elements --project modok
```

Reads each module's source files from the registry and extracts code identifiers — class names, method names, signal names — using AST parsing for Python and regex for C/C++. Writes the result to `registries/elements.yml`. These identifiers are forwarded to the LLM during `modok retrieve` so it can match ticket language to module slugs even when the ticket doesn't use the exact module name (e.g. "reinit button" → module containing `reinit_requested`).

Re-run this any time module source files are added, removed, or substantially renamed. Does not require Quine to be running.

---

## Step 6 — Verify the graph

`recall`/`diagnose` need an exact feature or module slug — if you don't already know one, list what's registered first:

```bash
modok list --project modok
```

Prints every registered feature and module slug (alphabetically, with names). Reads the registries directly — no Quine query, so it works even before `modok quine start`.

Then verify the graph with a real slug from that list, e.g.:

```bash
modok recall --project modok --module retrieval
```

Returns a summary of what MODOK knows about that module: its parent feature, source files, and test files.

---

## What's next

- **`docs/standing-query-demo.md`** — turn on `modok serve` (webhook push or GitHub poll adapter) so new tickets are ingested continuously, and watch MODOK detect an actionable pattern automatically without a manual `retrieve` call.
- **`docs/customize-for-your-project.md`** — the knobs worth deliberately setting up before relying on this day to day: registry curation quality (it directly affects debug-packet ranking), GitHub issue labels (required for the `new-bug-report-pattern` standing query), and write-back config.
- **Demo UI** — a local Next.js console showing the core MODOK workflow end to end (below).

---

## Demo UI

The demo UI is a local Next.js console that shows the core MODOK workflow: open a ticket, build a debug packet, see the result.

**Prerequisites:** Node.js 18+

**Install dependencies (first time only):**

```bash
cd ~/github/modok/ui
npm install
```

**Edit `ui/config.json`** to point at your project:

```json
{
  "project_slug": "modok",
  "modok_source": "demo-crm"
}
```

`project_slug` must match a `[[projects]]` slug in `~/.modok/config.toml`. `modok_source` is a label used in the rendered ticket markdown sent to `modok ingest`.

**Launch with real Quine (Quine must be running):**

```bash
npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

**Launch in mock mode (no Quine required):**

```bash
MODOK_MOCK=1 npm run dev
```

Mock mode returns the fixture packet from `ui/data/mock-debug-packets.json` without spawning any `modok` subprocesses — useful for demos and offline development.

**Run unit tests:**

```bash
npm test
```
