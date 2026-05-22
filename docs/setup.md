# MODOK Setup Guide

Complete bootstrap for a new machine — dev desktop or shared Mac mini. Follow these steps in order.

---

## What you need on disk

Before MODOK is useful you need three things cloned locally:

1. **The `modok` repo** — the MODOK tool itself
2. **Your project repo(s)** — the demo will use MODOK itself — currently MODOK validates file paths against disk at ingest time

MODOK never stores full file contents. It stores pointers and relationships. But it needs the files present to confirm they exist and assign high confidence scores.

---

## Prerequisites

**Java 17+** (for Quine):

```bash
java -version
```

Install if needed:

macOS:
```bash
brew install openjdk@21
echo 'export PATH="/opt/homebrew/opt/openjdk@21/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

Ubuntu/Debian (including WSL):
```bash
sudo apt install openjdk-21-jdk
```

**Python 3.11+** (for MODOK):

```bash
python3 --version
```

Install if needed:

macOS:
```bash
brew install python@3.12
```

Ubuntu/Debian (including WSL):
```bash
sudo apt install python3 python3-pip python3-full python3.12-venv
```

**git** (already present on most machines):

```bash
git --version
```

---

## Step 1 — Clone the modok repo

```bash
git clone https://github.com/marks/modok ~/github/modok
```

**macOS:**
```bash
cd ~/github/modok
pip install -e ".[dev]"
```

**Ubuntu/Debian/WSL** (system Python is externally managed — use a venv):
```bash
python3 -m venv ~/.venv/modok
source ~/.venv/modok/bin/activate
cd ~/github/modok
pip install -e ".[dev]"
```

Add `source ~/.venv/modok/bin/activate` to your `~/.bashrc` so it activates automatically in new shells.

Verify:

```bash
modok --version
```

---

## Step 2 — Choose a project repo to index

MODOK needs the project repo on disk to validate file references in registries and docs. The repo does not need to be built — just cloned.

**If you are following this guide for the first time**, use the `modok` repo itself as your sample project — it is already on disk from Step 1. It is fully self-contained (Python source, docs, tests) and gives you a real codebase to index without touching anything you care about.

**To index your own project**, clone it now:

```bash
git clone https://github.com/yourorg/yourproject ~/github/yourproject
```

The rest of this guide uses `modok` as the sample project slug and `~/github/modok` as the repo path. Substitute your own values if you are indexing a different repo.

---

## Step 3 — Create the MODOK runtime directory

```bash
mkdir -p ~/.modok/data
```

---

## Step 4 — Download the Quine JAR

```bash
curl -L -o ~/.modok/quine.jar \
  https://github.com/thatdot/quine/releases/download/v1.10.0/quine-1.10.0.jar
```

Verify:

```bash
java -jar ~/.modok/quine.jar --version
```

---

## Step 5 — Create the Quine config

```bash
cat > ~/.modok/quine.conf << EOF
quine {
  webserver {
    address = "127.0.0.1"
    port = 8080
  }

  store {
    type = rocks-db
    filepath = "$HOME/.modok/data/quine.db"
    create-parent-dir = yes
  }

  persistence {
    journal-enabled = true
    snapshot-schedule = on-node-sleep
  }
}
EOF
```

Note: Quine does not expand `~` in HOCON paths. The heredoc above uses `$HOME` which the shell expands before writing the file.

---

## Step 6 — Install a local LLM backend

MODOK uses a local LLM for metadata proposals (`--fix`) and ticket classification. Two backends are supported:

### Option A — Ollama (default, cross-platform)

**macOS:**
```bash
brew install ollama
brew services start ollama
```

**Ubuntu/Debian/WSL:**
```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama serve &   # or configure as a systemd service
```

Pull a model (gemma4 is a good default; llama3.2 also works):
```bash
ollama pull gemma4
```

Verify:
```bash
curl http://localhost:11434/api/tags
```

### Option B — oMLX (macOS Apple Silicon, OpenAI-compatible)

oMLX runs MLX-format models directly on the Metal GPU and exposes an OpenAI-compatible endpoint. No separate server process needed — it runs on demand.

Install and start:
```bash
brew install omlx        # or follow oMLX install instructions
omlx serve --model <your-model> --port 10240
```

Verify:
```bash
curl http://localhost:10240/v1/models
```

Use `protocol = "openai"` in your config (Step 7) when using oMLX.

---

## Step 7 — Create the MODOK config

Choose the config that matches the LLM backend you set up in Step 6.

**Option A — Ollama:**

```bash
cat > ~/.modok/config.toml << 'EOF'
[quine]
url = "http://127.0.0.1:8080"
jar = "~/.modok/quine.jar"

# LLM backends — tried in order. protocol: "ollama" (native /api/chat) or
# "openai" (OpenAI-compatible /chat/completions).
[[llm.backends]]
name     = "local"
protocol = "ollama"
endpoint = "http://localhost:11434"
model    = "gemma4"

[llm]
mode             = "auto"   # "auto" = escalate to next backend on validation failure
cegis_fix_enabled = true

# Optional: add a cloud fallback when local output fails schema validation.
# [[llm.backends]]
# name     = "cloud"
# protocol = "openai"
# endpoint = "https://api.anthropic.com/v1"
# model    = "claude-haiku-4-5-20251001"
# api_key  = ""   # or set MODOK_LLM_API_KEY env var

# Optional: emit rejected-field counterexamples as YAML fixtures for offline eval.
# counterexample_fixture_dir = "~/github/modok/tests/fixtures/llm_gateway"

# Performance tuning for local models on constrained hardware (e.g. MacBook Air).
# skip_summary skips the LLM summarise_packet call; the ticket subject is used instead.
# This saves one full LLM round-trip (~5–10s) at the cost of the generated summary sentence.
# skip_summary = true

[[projects]]
slug = "modok"
repo = "~/github/modok"
EOF
```

**Option B — oMLX (or any OpenAI-compatible local server):**

```bash
cat > ~/.modok/config.toml << 'EOF'
[quine]
url = "http://127.0.0.1:8080"
jar = "~/.modok/quine.jar"

[[llm.backends]]
name     = "local-mlx"
protocol = "openai"
endpoint = "http://localhost:10240"   # adjust to your oMLX port
model    = "your-model-name"

[llm]
mode              = "auto"
cegis_fix_enabled = true

[[projects]]
slug = "modok"
repo = "~/github/modok"
EOF
```

Add a `[[projects]]` block for each project repo you want MODOK to ingest.

---

## Step 8 — Start Quine

**Dev machine (manual):**

```bash
java -Dconfig.file=$HOME/.modok/quine.conf -jar ~/.modok/quine.jar
```

You should see:

```
Graph is ready
Quine web server available at http://127.0.0.1:8080
```

Leave this running in a terminal, or background it. Once `modok quine start` is implemented you can use that instead.

**Verify connectivity:**

```bash
curl -s http://127.0.0.1:8080/api/v1/query/cypher \
  -H "Content-Type: application/json" \
  -d '{"text": "RETURN 1"}' | python3 -m json.tool
```

Expected:

```json
{
  "columns": ["1"],
  "results": [[1]]
}
```

---

## Step 9 — Initialize a project

```bash
modok init --project modok --repo ~/github/modok
```

This:
- Registers the project in `~/.modok/config.toml` if not already present
- Installs a git post-commit hook in the repo that runs ingestion automatically on commits touching docs, registries, or tickets
- Creates empty stub files for `registries/features.yml`, `registries/modules.yml`, `registries/errors.yml`, and `registries/doc-types.yml` if they don't already exist

---

## Step 10 — Generate the code map

The code map is a YAML snapshot of every file in the repo — language, role, symbols, and SHA256. It is the foundation for registry bootstrap and file validation.

```bash
modok extract-code-map --project modok --repo ~/github/modok
```

Output is written to `<repo>/.modok/code-map.yml` (gitignored). Re-run this any time the repo's file structure changes significantly.

```
Extracted code map: 320 files (71 source, 68 test, 84 config, 97 docs) → /path/to/modok/.modok/code-map.yml
```

---

## Step 11 — Bootstrap registries from arrow docs

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

## Step 12 — Run first ingestion

Run the three ingestion commands **in this order**. Each one builds on the previous:

1. `modok ingest` — reads docs and writes the static knowledge graph (Feature, Module, File, DocSection nodes and their edges). Must run first because the other two commands depend on File nodes being present.
2. `modok ingest-git` — walks git log and writes Commit nodes with `TOUCHES` edges to File nodes. Requires File nodes from step 1.
3. `modok ingest-elements` — extracts code identifiers from source files and writes `registries/elements.yml`. Does not touch the graph; enriches the registry used by `modok retrieve` at query time.

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

## Step 13 — Verify the graph

```bash
modok recall --project modok --module retrieval
```

Returns a summary of what MODOK knows about that module: its parent feature, source files, and test files.

---

## Persistent Quine service (macOS only)

On the shared Mac mini, run Quine as a launchd service so it starts on boot and restarts on crash.

On Linux/WSL, use systemd or simply run Quine manually in a terminal for development. WSL does not support launchd.

**Create the plist** (replace `YOURUSER` with `whoami` output):

```bash
cat > ~/Library/LaunchAgents/io.modok.quine.plist << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>io.modok.quine</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/java</string>
    <string>-Dconfig.file=$HOME/.modok/quine.conf</string>
    <string>-jar</string>
    <string>$HOME/.modok/quine.jar</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$HOME/.modok/quine.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/.modok/quine.log</string>
</dict>
</plist>
EOF
```

**Load and verify:**

```bash
launchctl load ~/Library/LaunchAgents/io.modok.quine.plist
launchctl list | grep modok
curl -s http://127.0.0.1:8080/api/v1/query/cypher \
  -H "Content-Type: application/json" \
  -d '{"query": "RETURN 1"}'
```

**Logs:**

```bash
tail -f ~/.modok/quine.log
```

**Stop / disable:**

```bash
launchctl stop io.modok.quine          # stops; launchd restarts it (KeepAlive)
launchctl unload ~/Library/LaunchAgents/io.modok.quine.plist  # stops and disables
```

---

## What lives where

| Location | Contents |
|---|---|
| `~/github/modok/` | MODOK source code, tests, LID docs |
| `~/github/modok/registries/` | Feature, module, error registries and `elements.yml` (version-controlled in project repo) |
| `~/github/modok/.modok/code-map.yml` | Per-project code map — files, roles, symbols, hashes (local, gitignored) |
| `~/.modok/config.toml` | MODOK runtime config — Quine URL, project repo paths, LLM config |
| `~/.modok/quine.conf` | Quine HOCON config |
| `~/.modok/quine.jar` | Quine binary |
| `~/.modok/data/quine.db` | RocksDB graph store |
| `~/.modok/quine.log` | Quine stdout/stderr |
| `~/.modok/quine.pid` | PID file (written by `modok quine start`) |

Nothing under `~/.modok/` belongs in any git repo.

---

## Storage

| Workload | Approximate graph size |
|---|---|
| Single project (stagehand) | < 500 MB |
| 5–10 projects | 1–5 GB |
| Comfortable headroom | 20 GB |

---

## Backup

The graph is fully reconstructible by re-running ingestion. To snapshot:

```bash
# Stop Quine first
launchctl unload ~/Library/LaunchAgents/io.modok.quine.plist
cp -r ~/.modok/data ~/.modok/data.bak
launchctl load ~/Library/LaunchAgents/io.modok.quine.plist
```

---

## Upgrading Quine

```bash
curl -L -o ~/.modok/quine.jar \
  https://github.com/thatdot/quine/releases/download/vNEW_VERSION/quine-NEW_VERSION.jar
# restart Quine
```

Check thatDot release notes for schema migration requirements before upgrading a populated database.

---

## Upgrading MODOK

```bash
cd ~/github/modok
git pull
pip install -e ".[dev]"
```

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
