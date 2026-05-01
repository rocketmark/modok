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

## Step 6 — Create the MODOK config

```bash
cat > ~/.modok/config.toml << 'EOF'
[quine]
url = "http://127.0.0.1:8080"
jar = "~/.modok/quine.jar"

[llm]
# Local model via Ollama (default). Must be running before ingestion.
provider = "ollama"
base_url = "http://127.0.0.1:11434/v1"
model = "llama3"

# Uncomment to add a remote escalation target.
# [llm.remote]
# provider = "anthropic"
# model = "claude-sonnet-4-6"
# api_key_env = "ANTHROPIC_API_KEY"

[[projects]]
slug = "stagehand"
repo = "~/github/stagehand"
EOF
```

Add a `[[projects]]` block for each project repo you want MODOK to ingest.

---

## Step 7 — Start Quine

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

## Step 8 — Initialize a project

```bash
modok init --project stagehand --repo ~/github/stagehand
```

This:
- Registers the project in `~/.modok/config.toml` if not already present
- Installs a git post-commit hook in the stagehand repo that runs ingestion automatically on commits touching docs, registries, or tickets
- Validates that `registries/features.yml`, `registries/modules.yml`, and `registries/errors.yml` exist in the repo (creates stubs if missing)

---

## Step 9 — Run first ingestion

```bash
modok ingest --project stagehand ~/github/stagehand
```

This discovers all markdown and YAML files under the repo root, parses MODOK frontmatter and blocks, and writes nodes and edges to Quine. You should see a structured report:

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
  Files skipped:   18
  Duration:        1.4s
```

---

## Step 10 — Verify the graph

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
