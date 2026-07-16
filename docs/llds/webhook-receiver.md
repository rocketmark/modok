# Webhook Receiver

## Context and Design Philosophy

The webhook receiver is MODOK's streaming ingestion surface. It runs as a small HTTP server (`modok serve`) that accepts push events from external tools — GitHub webhooks, trouble ticket systems, automation platforms — and routes them into the existing ingestion pipeline. No new graph logic. No new node types. The write path is identical to `modok ingest-github` and `modok ingest`'s ticket-file path; the webhook receiver is purely a new *entry point* into that path.

The same design principle as the CLI and MCP server applies: **this layer is a thin adapter.** Each handler is ≤ 40 lines: verify the request → normalize the payload → call existing core → return a response. All graph logic lives downstream.

This component is explicitly a **reference design for streaming ingestion**. The adapter pattern is the thing being demonstrated. Adding a new source — Jira, Linear, PagerDuty, Redis Streams — requires implementing one adapter protocol and registering it. Nothing in the server, router, pipeline, or graph changes.

## Adapter Pattern

Two adapter protocols cover the two ingestion topologies: **push** (something calls MODOK) and **pull** (MODOK polls or consumes from a stream).

### Push adapter (`PushAdapter`)

Used for HTTP webhook sources: GitHub, Jira, Linear, any system that POSTs to MODOK.

```python
# src/modok/webhook/adapters/base.py

class PushAdapter(Protocol):
    def verify_request(self, request: Request, config: WebhookConfig) -> None:
        """Raise WebhookAuthError if the request cannot be verified."""
        ...

    def normalize_event(self, payload: dict, event_type: str) -> IngestEvent | None:
        """Map source payload to IngestEvent. Return None to silently skip."""
        ...
```

### Pull adapter (`PullAdapter`)

Used for consumer sources: Redis Streams, SQS, Kafka (if ever needed). The adapter owns its own connection and blocking loop; MODOK calls `start()` on server startup and `stop()` on shutdown.

```python
class PullAdapter(Protocol):
    async def start(self, config: WebhookConfig, on_event: Callable[[IngestEvent], Awaitable[None]]) -> None:
        """Connect to the source and begin consuming. Call on_event for each event."""
        ...

    async def stop(self) -> None:
        """Gracefully disconnect."""
        ...
```

The `on_event` callback is the same `run_ingest_event` path that push adapters use. The router owns both protocol types; the ingestion pipeline sees neither.

### `IngestEvent` — the common currency

```python
# src/modok/webhook/models.py

@dataclass
class IngestEvent:
    kind: Literal["customer_issue", "fix", "skip"]
    project_slug: str
    data: CustomerIssueData | FixData | None   # None when kind="skip"
```

`CustomerIssueData` and `FixData` are the same structures `ingest-github` already writes to Quine. Any adapter that produces `IngestEvent` is immediately compatible with the full graph.

### Registration

```python
# src/modok/webhook/router.py

PUSH_ADAPTERS: dict[str, PushAdapter] = {
    "github": GitHubAdapter(),
    "ticket": GenericTicketAdapter(),
    # "jira":  JiraAdapter(),    ← add here; see docs/llds/webhook-receiver.md § Jira adapter
    # "linear": LinearAdapter(), ← add here; see docs/llds/webhook-receiver.md § Linear adapter
}

PULL_ADAPTERS: dict[str, PullAdapter] = {
    # "redis": RedisStreamsAdapter(),  ← add here; see docs/llds/webhook-receiver.md § Redis Streams adapter
}
```

That is the complete extension surface. No base classes to inherit, no framework plugin system — two dicts and two protocols.

---

## Implemented Adapters

### GitHub adapter (`adapters/github.py`)

**Verification**

GitHub signs every delivery with `X-Hub-Signature-256: sha256=<hmac>`. The adapter computes `HMAC-SHA256(body_bytes, secret)` using `hmac.compare_digest` (constant-time). Missing header or mismatch → `WebhookAuthError` → HTTP 401.

The `ping` event (sent when a webhook is first registered on GitHub) is handled explicitly: MODOK verifies the HMAC so the operator gets immediate feedback on a misconfigured secret, then returns 200 with no graph write.

Secret configured at `[webhook] github_secret` in `~/.modok/config.toml`.

**Event routing**

| `X-GitHub-Event` | `action` | Result |
|---|---|---|
| `ping` | — | 200, HMAC verified, no graph write |
| `issues` | `opened` | `CustomerIssue` node |
| `issues` | `edited`, `labeled`, `closed`, `reopened` | re-upsert (idempotent) |
| `pull_request` | `closed` + `merged: true` | `Fix` node |
| `pull_request` | any other | `kind="skip"` |
| anything else | — | `kind="skip"` |

**Field mapping** — same as `ingest-github`:

| `IngestEvent` field | GitHub source |
|---|---|
| `ticket_id` | `str(issue.number)` |
| `summary` | `issue.title` |
| `raw_text` | `issue.body` or `""` |
| `status` | `"open"` / `"closed"` |
| `fix_id` | `"gh-" + str(pr.number)` |

---

### Generic ticket adapter (`adapters/ticket.py`)

**Verification**

Bearer token in `Authorization: Bearer <token>` header. Token configured at `[webhook] bearer_token`. Missing or non-matching → `WebhookAuthError` → HTTP 401.

**Payload schema**

```json
{
  "ticket_id":     "TKT-442",
  "summary":       "Tracker dropout after 20 minutes",
  "body":          "Customer reports pose dropout...",
  "source_system": "internal"
}
```

`ticket_id` and `summary` are required. `body` and `source_system` are optional (defaults: `""` and `"webhook"`). Unknown fields are silently ignored. Validated via pydantic.

---

## Planned Adapters (not yet implemented)

These document the extension point for future contributors. Each requires one new file under `adapters/` and one dict entry in `router.py`. Nothing else changes.

### Redis Streams adapter (`adapters/redis_streams.py`)

**Topology**: pull. MODOK connects to Redis on startup and reads from a named stream.

**Config additions** to `[webhook]` block:

```toml
[webhook.redis]
url    = "redis://localhost:6379"
stream = "modok-events"
group  = "modok-consumer"           # consumer group name
consumer = "modok-1"                # consumer name within the group
block_ms = 5000                     # XREADGROUP block timeout in ms
```

**Implementation sketch**:

```python
class RedisStreamsAdapter:  # implements PullAdapter
    async def start(self, config, on_event):
        # 1. Connect via redis.asyncio
        # 2. XGROUP CREATE stream group $ MKSTREAM (idempotent)
        # 3. Loop: XREADGROUP GROUP group consumer COUNT 10 BLOCK block_ms STREAMS stream >
        # 4. For each message: normalize_event(msg) → on_event(event)
        # 5. XACK stream group message_id  (after on_event completes)
        ...

    async def stop(self):
        # Cancel the consumer loop; disconnect
        ...
```

**Message format** — MODOK expects each Redis stream entry to be a hash with at least:

```
ticket_id     TKT-442
summary       Tracker dropout after 20 minutes
body          Customer reports pose dropout...
source_system internal
```

Same fields as the generic ticket adapter. Any Redis producer that writes these fields is compatible.

**Dependency**: `redis[asyncio]>=5.0` — add to `pyproject.toml` as an optional dependency under `[project.optional-dependencies] redis`.

**ACK discipline**: MODOK ACKs (`XACK`) only after `on_event` completes successfully. A pipeline failure leaves the message unacknowledged; it will be redelivered on the next `XREADGROUP` call (Redis pending entry list). This gives at-least-once delivery without an external retry mechanism.

---

### Jira adapter (`adapters/jira.py`)

**Topology**: push. Jira sends webhook POSTs to MODOK.

**Verification**: Jira does not support HMAC signing on its webhook deliveries (as of Jira Cloud 2024). Use IP allowlist verification instead — Jira publishes its egress IP ranges. Alternatively, configure Jira to include a shared secret in a custom header (`X-Modok-Token`) and verify as a bearer token.

Recommended: custom header approach. Config:

```toml
[webhook.jira]
shared_secret = "..."
```

**Event routing**:

| Jira event | Action | Result |
|---|---|---|
| `jira:issue_created` | — | `CustomerIssue` node |
| `jira:issue_updated` | — | re-upsert |
| `jira:issue_deleted` | — | no-op (MODOK does not delete nodes) |

**Field mapping**:

| `IngestEvent` field | Jira source |
|---|---|
| `ticket_id` | `issue.key` (e.g. `"PROJ-123"`) |
| `summary` | `issue.fields.summary` |
| `raw_text` | `issue.fields.description` (may be Atlassian Document Format — strip to plain text) |
| `status` | `"open"` if resolution is null, else `"closed"` |
| `source_system` | `"jira"` |

**ADF stripping**: Jira Cloud descriptions arrive as Atlassian Document Format (ADF) JSON, not plain text. The adapter must walk the `content` tree and concatenate `text` nodes. A ~20-line recursive function handles the common cases; edge cases (tables, code blocks) are flattened to `[table]` / `[code]` placeholders.

---

### Linear adapter (`adapters/linear.py`)

**Topology**: push. Linear sends webhook POSTs to MODOK.

**Verification**: Linear signs deliveries with `Linear-Signature: <hmac-sha256>`. Same HMAC-SHA256 pattern as GitHub. Config:

```toml
[webhook.linear]
signing_secret = "..."
```

**Event routing**:

| Linear event type | Action | Result |
|---|---|---|
| `Issue` | `create` | `CustomerIssue` node |
| `Issue` | `update` | re-upsert |
| `Issue` | `remove` | no-op |

**Field mapping**:

| `IngestEvent` field | Linear source |
|---|---|
| `ticket_id` | `data.identifier` (e.g. `"ENG-442"`) |
| `summary` | `data.title` |
| `raw_text` | `data.description` (Markdown — store as-is) |
| `status` | `"open"` if `data.completedAt` is null, else `"closed"` |
| `source_system` | `"linear"` |

**Dependency**: none beyond `httpx`. Linear's payload is clean JSON with no special encoding.

---

## Endpoints

```
POST /webhook/{project_slug}/{source}   — dispatch to registered push adapter
GET  /health                            — liveness check
```

`{source}` maps to a key in `PUSH_ADAPTERS`. Unknown slug → 404. Unknown source → 404 (not 400: the path segment doesn't exist, not a malformed request).

`/health` returns:
```json
{"status": "ok", "quine": true}
```
`quine` reflects the last Quine ping result (not a live ping per health check — cached from the startup ping and periodic background check every 30s).

## Request Lifecycle

```
POST /webhook/{slug}/{source}
  │
  ├─ 1. Resolve project slug → ModokConfig entry; 404 if unknown
  ├─ 2. Look up adapter in PUSH_ADAPTERS by source; 404 if unknown or not in enabled_sources
  ├─ 3. Read raw body bytes (before any JSON parsing)
  ├─ 4. adapter.verify_request(request, config) → 401 on WebhookAuthError
  ├─ 5. Parse JSON body from raw bytes → 400 on malformed JSON
  ├─ 6. adapter.normalize_event(payload, event_type) → IngestEvent
  │      └─ kind="skip" → return 200 {"status": "skipped"}
  ├─ 7. asyncio.to_thread(run_ingest_event, event, quine_client)
  │      └─ calls existing pipeline; upsert via idFrom (idempotent)
  │      └─ Quine unreachable → 500 (GitHub retries on non-2xx)
  │      └─ on success: update cached Quine status to true
  └─ 8. 200 {"status": "ok", "nodes_written": N}
       or 500 {"status": "error", "detail": "..."} on pipeline failure
```

Step 6 runs ingestion in a thread pool (`asyncio.to_thread`) so it does not block the event loop. GitHub expects a 200 within 10 seconds; the pipeline is fast enough for single-event payloads.

Quine down → 500. GitHub retries on non-2xx (3 attempts over ~1hr), so the event is not lost during a brief Quine outage.

## `modok serve` Command

```bash
modok serve [--port 4242] [--host 127.0.0.1]
```

**Startup sequence**:
1. Load `~/.modok/config.toml`
2. Validate webhook config — hard-fail if any enabled source is missing its required secret/token:
   - `github` endpoint enabled but `github_secret` not set → exit 1: "github_secret not configured — set [webhook] github_secret or disable the github source"
   - `ticket` endpoint enabled but `bearer_token` not set → exit 1: "bearer_token not configured — set [webhook] bearer_token or disable the ticket source"
3. Ping Quine → exit 2 if unreachable
4. Start pull adapters (if any configured)
5. Bind and serve

Logs to stderr. No stdout output after startup (structured events go to the delivery log if configured).

Config section in `~/.modok/config.toml`:

```toml
[webhook]
port = 4242
enabled_sources = ["github", "ticket"]   # omit to enable all implemented push adapters
github_secret = "..."    # required when "github" is in enabled_sources
bearer_token  = "..."    # required when "ticket" is in enabled_sources

# Redis Streams (optional — requires pip install modok[redis])
# [webhook.redis]
# url    = "redis://localhost:6379"
# stream = "modok-events"
# group  = "modok-consumer"
# consumer = "modok-1"

# Jira (optional)
# [webhook.jira]
# shared_secret = "..."

# Linear (optional)
# [webhook.linear]
# signing_secret = "..."
```

## Security

- **GitHub**: HMAC-SHA256, `hmac.compare_digest` (constant-time)
- **Generic ticket**: bearer token
- **Jira**: custom header shared secret (bearer-token pattern)
- **Linear**: HMAC-SHA256, same pattern as GitHub
- **Redis Streams**: Redis AUTH via connection URL; no per-message signing
- **All HTTP sources**: bind to `127.0.0.1` by default — not network-exposed without deliberate config
- **TLS**: out of scope for this layer. Use nginx or Caddy as a reverse proxy for external exposure.

## Framework

FastAPI + uvicorn. FastAPI fits the existing async pattern; pydantic v2 is already a dependency.

New production dependencies: `fastapi>=0.110`, `uvicorn>=0.29`.
Optional dependency: `redis[asyncio]>=5.0` (only needed for Redis Streams adapter).

## Module Layout

```
src/modok/webhook/
    __init__.py
    server.py              # FastAPI app, route registration, startup sequence
    router.py              # PUSH_ADAPTERS + PULL_ADAPTERS dicts, dispatch logic
    models.py              # IngestEvent, CustomerIssueData, FixData, WebhookConfig
    errors.py              # WebhookAuthError, WebhookNotFoundError
    adapters/
        __init__.py
        base.py            # PushAdapter + PullAdapter protocols
        github.py          # GitHubAdapter
        ticket.py          # GenericTicketAdapter
        # redis_streams.py ← add when implementing Redis adapter
        # jira.py          ← add when implementing Jira adapter
        # linear.py        ← add when implementing Linear adapter
src/modok/cli/commands/
    serve.py               # modok serve command (thin; calls webhook.server.start())
```

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|---|---|---|---|
| Framework | FastAPI + uvicorn | aiohttp, Flask | FastAPI fits existing async pattern; pydantic v2 already a dependency |
| Port | 4242 | 8080 (Quine), 8181 (MCP) | Avoids both existing services |
| Auth — GitHub | HMAC-SHA256 + `hmac.compare_digest` | IP allowlist | HMAC is GitHub's standard; IP-based is fragile with cloud runners |
| Auth — generic ticket | Bearer token | API key query param | Bearer token is HTTP-native; query params appear in logs |
| Auth — Jira | Custom header shared secret | HMAC (not supported by Jira Cloud) | Jira Cloud does not sign webhook deliveries; custom header is the next-best option |
| Auth — Linear | HMAC-SHA256 | Bearer token | Linear provides signing; use it |
| Push vs pull protocol split | Two separate protocols (`PushAdapter`, `PullAdapter`) | Single adapter interface with optional HTTP request arg | HTTP `Request` objects have no meaning for consumer adapters; faking them is a leaky abstraction and a one-way door — splitting now costs nothing |
| Unknown source response | 404 | 400 | Source name is a path segment; unknown path = not found, not malformed request. Clearer in GitHub delivery logs |
| Quine-down response | 500 | 200 (drop silently) | 500 causes GitHub to retry automatically; silent 200 loses the event |
| Missing secret at startup | Hard-fail (exit 1) for enabled sources only | Runtime 503; hard-fail all sources | Operator misconfiguration caught at startup; but forcing a dummy secret for a disabled source is unnecessary friction |
| `enabled_sources` config | Optional list; absent = all adapters active | Always require explicit list | Omitting is the common case (run everything); explicit list is the escape hatch for single-source deployments |
| `ping` event handling | Verify HMAC, return 200, no graph write | Skip verification | Verifying HMAC on `ping` gives the operator immediate feedback on secret misconfiguration |
| Raw body read order | Read raw bytes before JSON parse; pass raw bytes to `verify_request` | Parse JSON first, re-encode for HMAC | Re-encoding JSON is not byte-for-byte identical to the original; HMAC must be computed on the original wire bytes |
| `/health` Quine cache update | Update cached status to `true` on successful pipeline call | Only update via periodic ping | A successful pipeline call is proof Quine is reachable; not updating would leave the health endpoint stale until the next 30s ping |
| Sync vs async ingestion | `asyncio.to_thread` | Async queue + background worker | Matches MCP server pattern; sufficient for low-volume webhook events; queue adds complexity without benefit at this scale |
| Redis ACK discipline | ACK after `on_event` completes | ACK on receipt | At-least-once delivery without external retry infrastructure |
| TLS | Caller's responsibility (reverse proxy) | Built-in TLS via uvicorn | Reverse proxy (nginx/Caddy) is the standard pattern; built-in TLS complicates cert management |

## Open Questions & Future Decisions

1. **Delivery log** — an append-only log of received events (timestamp, source, project, status). Useful for debugging missed events when the server is externally exposed. Defer until external exposure is needed.
2. **Async pipeline** — if ingestion becomes slow, replace `asyncio.to_thread` with a bounded async queue and background worker. `IngestEvent` is already the natural queue payload.
3. **TLS / external exposure** — if the server needs to receive GitHub webhooks directly (not via a local forwarder), document the nginx/Caddy reverse proxy setup in `docs/setup.md`. Defer until someone needs external exposure.
4. **`modok serve` as a launchd/systemd service** — for always-on ingestion on the Mac mini, document a launchd plist. Follows the same pattern as the Quine plist in `docs/setup.md`.
5. **Rate limiting** — GitHub retries are the only external pressure in local use. Add a reverse-proxy-level rate limit if the server is externally exposed.

## References

- `docs/llds/ingestion-pipeline.md` — `run_ingestion` entry point; idempotent write behavior
- `docs/llds/github-ingestion.md` — field mappings reused by the GitHub adapter
- `docs/llds/mcp-server.md` — parallel thin-surface pattern; `asyncio.to_thread` usage
- `docs/llds/cli.md` — `modok serve` follows CLI conventions (exit codes, Quine ping on startup)
