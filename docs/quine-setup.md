# Quine Setup and Operations

Quine is MODOK's graph store. It runs as a standalone JAR process; MODOK connects to it over HTTP.

## Prerequisites

Java 17 or newer is required.

```bash
java -version
```

On macOS, install via Homebrew if needed:

```bash
brew install openjdk@21
```

Follow the Homebrew instructions to add it to your PATH (typically `echo 'export PATH="/opt/homebrew/opt/openjdk@21/bin:$PATH"' >> ~/.zshrc`).

---

## Step 1 — Create the MODOK directory

```bash
mkdir -p ~/.modok/data
```

---

## Step 2 — Download the Quine JAR

```bash
curl -L -o ~/.modok/quine.jar \
  https://github.com/thatdot/quine/releases/download/v1.10.0/quine-1.10.0.jar
```

Verify the download:

```bash
java -jar ~/.modok/quine.jar --version
```

---

## Step 3 — Create the Quine config

```bash
cat > ~/.modok/quine.conf << 'EOF'
quine {
  webserver {
    address = "127.0.0.1"
    port = 8080
  }

  store {
    type = rocks-db
    filepath = "/Users/YOURUSER/.modok/data/quine.db"
    create-parent-dir = yes
  }

  persistence {
    journal-enabled = true
    snapshot-schedule = on-node-sleep
  }
}
EOF
```

Replace `/Users/YOURUSER` with your actual home directory path (`echo $HOME`). Quine does not expand `~` in HOCON paths.

---

## Step 4 — Create the MODOK config

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
EOF
```

---

## Step 5 — Start Quine

### Manual (dev machine)

Run Quine in a terminal (or background it with `&`):

```bash
java -Dconfig.file=$HOME/.modok/quine.conf -jar ~/.modok/quine.jar
```

You should see output like:

```
Graph is ready
Quine web server available at http://127.0.0.1:8080
```

### Via MODOK CLI (once installed)

```bash
modok quine start    # starts the JAR in the background, writes PID to ~/.modok/quine.pid
modok quine stop     # sends SIGTERM to the PID
modok quine status   # reports running/stopped and whether HTTP is reachable
```

---

## Step 6 — Verify connectivity

```bash
curl -s http://127.0.0.1:8080/api/v1/query/cypher \
  -H "Content-Type: application/json" \
  -d '{"query": "RETURN 1"}' | python3 -m json.tool
```

Expected response:

```json
{
  "columns": ["1"],
  "results": [[1]]
}
```

MODOK also `ping()`s Quine at startup and prints a clear error with remediation steps if it is unreachable.

---

## Mac mini — launchd persistent service

On the shared Mac mini, Quine runs as a launchd service so it starts on boot and restarts on crash.

**Step 1** — Create the plist. Replace `YOURUSER` with the Mac mini's username (`whoami`):

```bash
cat > ~/Library/LaunchAgents/io.modok.quine.plist << 'EOF'
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
    <string>-Dconfig.file=/Users/YOURUSER/.modok/quine.conf</string>
    <string>-jar</string>
    <string>/Users/YOURUSER/.modok/quine.jar</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/Users/YOURUSER/.modok/quine.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/YOURUSER/.modok/quine.log</string>
</dict>
</plist>
EOF
```

**Step 2** — Load the service:

```bash
launchctl load ~/Library/LaunchAgents/io.modok.quine.plist
```

**Step 3** — Verify it's running:

```bash
launchctl list | grep modok
curl -s http://127.0.0.1:8080/api/v1/query/cypher \
  -H "Content-Type: application/json" \
  -d '{"query": "RETURN 1"}'
```

**To stop or restart:**

```bash
launchctl stop io.modok.quine    # stops; launchd will restart it (KeepAlive = true)
launchctl unload ~/Library/LaunchAgents/io.modok.quine.plist  # stops and disables
```

**Logs:**

```bash
tail -f ~/.modok/quine.log
```

---

## Storage

Quine's RocksDB store grows with the graph. Expected sizes for MODOK's static use case:

| Workload | Approximate size |
|---|---|
| Single project (stagehand) | < 500 MB |
| 5–10 projects | 1–5 GB |
| Comfortable headroom | 20 GB |

MODOK never stores full source files, full doc text, or raw logs in Quine — only pointers, summaries, and relationships.

---

## Backup

The graph is fully reconstructible by re-running ingestion against source docs and tickets, so Quine's data directory is not critical to back up. If you want a snapshot, stop Quine first then copy the directory:

```bash
launchctl unload ~/Library/LaunchAgents/io.modok.quine.plist   # or kill the manual process
cp -r ~/.modok/data ~/.modok/data.bak
launchctl load ~/Library/LaunchAgents/io.modok.quine.plist
```

---

## Upgrading Quine

Replace the JAR and restart:

```bash
curl -L -o ~/.modok/quine.jar \
  https://github.com/thatdot/quine/releases/download/vNEW_VERSION/quine-NEW_VERSION.jar
modok quine restart   # or manually stop/start
```

Check the thatDot release notes for schema migration requirements before upgrading on a populated database.
