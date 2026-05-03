# MODOK Setup Guide

Complete bootstrap for a new machine — dev desktop or shared Mac mini. Follow these steps in order.

---

## What you need on disk

Before MODOK is useful you need three things cloned locally:

1. **The `modok` repo** — the MODOK tool itself
2. **Your project repo(s)** — e.g. `stagehand` — because MODOK validates file paths against disk at ingest time

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

## Step 2 — Clone your project repo(s)

```bash
git clone https://github.com/marks/stagehand ~/github/stagehand
```

MODOK needs the project repo on disk to validate file references in registries and docs. The repo does not need to be built — just cloned.

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

## Step 6 — Install Ollama and pull a model

MODOK uses a local LLM for metadata proposals (`--fix`) and ticket classification. Ollama is the supported local backend.

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
curl http://localhost:11434/v1/models
```

---

## Step 7 — Create the MODOK config

```bash
cat > ~/.modok/config.toml << 'EOF'
[quine]
url = "http://127.0.0.1:8080"
jar = "~/.modok/quine.jar"

[llm]
# Local model via Ollama. Must be running before using --fix or ticket parsing.
local_endpoint = "http://localhost:11434/v1"
local_model = "gemma4"

# Enable the bounded CEGIS repair loop: if the LLM's first proposal fails
# verification, one repair attempt is made with the counterexamples as context.
cegis_fix_enabled = true

# Optional: escalate to a remote model when local output fails schema validation.
# remote_endpoint = "https://api.anthropic.com/v1"
# remote_model = "claude-sonnet-4-6"
# remote_api_key = ""   # or set MODOK_LLM_API_KEY env var

# Optional: emit rejected-field counterexamples as YAML fixtures for offline eval.
# counterexample_fixture_dir = "~/github/modok/tests/fixtures/llm_gateway"

[[projects]]
slug = "stagehand"
repo = "~/github/stagehand"
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
modok init --project stagehand --repo ~/github/stagehand
```

This:
- Registers the project in `~/.modok/config.toml` if not already present
- Installs a git post-commit hook in the stagehand repo that runs ingestion automatically on commits touching docs, registries, or tickets
- Creates empty stub files for `registries/features.yml`, `registries/modules.yml`, `registries/errors.yml`, and `registries/doc-types.yml` if they don't already exist

---

## Step 10 — Generate the code map

The code map is a YAML snapshot of every file in the repo — language, role, symbols, and SHA256. It is the foundation for registry bootstrap and file validation.

```bash
modok extract-code-map --project stagehand --repo ~/github/stagehand
```

Output is written to `<repo>/.modok/code-map.yml` (gitignored). Re-run this any time the repo's file structure changes significantly.

```
Extracted code map: 320 files (71 source, 68 test, 84 config, 97 docs) → /path/to/stagehand/.modok/code-map.yml
```

---

## Step 11 — Bootstrap registries from arrow docs

If the project has arrow docs (`docs/arrows/index.yaml`), use `import-arrow` to generate `registries/features.yml` and `registries/modules.yml` directly from them. This replaces the empty stubs created by `modok init`.

```bash
modok import-arrow --project stagehand --repo ~/github/stagehand
```

This reads each arrow doc's `### Code` and `### Key Components` sections, validates all file paths against the code map, and writes both registry files. Add `--no-llm` to skip name/description generation (useful for CI or first runs):

```bash
modok import-arrow --project stagehand --repo ~/github/stagehand --no-llm
```

Add `--dry-run` to preview the proposed registries without writing anything.

If the project does not have arrow docs, edit `registries/features.yml` and `registries/modules.yml` manually using the stubs created by `modok init`.

---

## Step 12 — Run first ingestion

**Ingest docs:**

```bash
modok ingest --project stagehand
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
modok ingest-git --project stagehand
```

Imports commits that touch registered source files and docs as `Commit` nodes in the graph, with `TOUCHES` edges to the relevant `File` nodes. By default imports the last 6 months / 500 commits. Use `--full` for an initial bootstrap of the full history, or `--since 2025-01-01` to import from a specific date. Subsequent runs are incremental — only commits since the last run are imported.

---

## Step 13 — Verify the graph

```bash
modok recall --project stagehand --feature shtp-receiver
```

Returns a summary of what MODOK knows about that feature: docs, modules, files, tests, known issues, risks.

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
| `~/github/stagehand/registries/` | Feature, module, error registries (version-controlled in stagehand repo) |
| `~/github/stagehand/.modok/code-map.yml` | Per-project code map — files, roles, symbols, hashes (local, gitignored) |
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
  "project_slug": "stagehand",
  "modok_source": "demo-crm"
}
```

`project_slug` must match a `[[projects]]` slug in `~/.modok/config.toml`. `modok_source` is the `--source` value passed to `modok retrieve`.

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
