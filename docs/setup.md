# MODOK Platform Setup

Gets the MODOK *platform* running on a machine — Quine, a local LLM backend, and the `modok` CLI itself. Nothing here is project-specific; follow this once per machine (dev desktop or shared Mac mini).

**Next step after this doc:** [`docs/project-setup.md`](project-setup.md) — adding a project repo and running its first ingestion.

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

## Step 1 — Clone and install MODOK

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

## Step 2 — Create the MODOK runtime directory

```bash
mkdir -p ~/.modok/data
```

Everything MODOK needs at runtime — Quine's JAR, its database, and MODOK's own config — lives under `~/.modok/`. Nothing under here belongs in any git repo.

---

## Step 3 — Download the Quine JAR

```bash
curl -L -o ~/.modok/quine.jar \
  https://github.com/thatdot/quine/releases/download/v1.10.0/quine-1.10.0.jar
```

Verify:

```bash
java -jar ~/.modok/quine.jar --version
```

---

## Step 4 — Create the Quine config

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

## Step 5 — Install a local LLM backend

MODOK uses a local LLM for registry bootstrap proposals (`modok init --assisted`, `modok normalise`), ticket classification, and anchor extraction. Two backends are supported.

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

Use `protocol = "openai"` in your config (Step 6) when using oMLX.

---

## Step 6 — Create the MODOK config

Choose the config that matches the LLM backend you set up in Step 5. This is the base config — Quine and LLM settings only. You'll add a `[[projects]]` entry per project in `docs/project-setup.md`.

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
EOF
```

---

## Step 7 — Start Quine and verify connectivity

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
| `~/.modok/config.toml` | MODOK runtime config — Quine URL, LLM config, and (after `docs/project-setup.md`) project repo paths |
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

## Next step

The platform is running but there's no project data yet. Continue to [`docs/project-setup.md`](project-setup.md) to add a project repo and run its first ingestion.
