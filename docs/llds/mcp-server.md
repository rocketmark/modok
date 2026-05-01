# MCP Server

## Context and Design Philosophy

The MODOK MCP server exposes the same operations as the CLI to agents via the Model Context Protocol. The primary callers are non-Claude agents — Codex, Ollama-driven agents, VS Code agents, and similar — that call tools over HTTP rather than via subprocess. Claude Desktop and Claude Code are secondary callers that use the stdio transport. Both transports are supported from the same tool definitions.

The server is a thin surface layer — identical to the CLI in that philosophy. No logic lives here. Each tool is ≤ 30 lines: validate inputs → call core → return result. The core components (`modok.retrieval`, `modok.ingestion`, `modok.quine`) are identical under both surfaces.

Guiding principles:

- **Thin.** No business logic. Each MCP tool delegates to the same core function the corresponding CLI command calls.
- **`project` required everywhere.** Every tool that touches the graph takes a `project` string argument. No ambient project state.
- **Quine-first startup.** Every graph-touching tool pings Quine before operating and raises a descriptive `McpError` if unreachable.
- **Typed return values.** Tools return structured dicts that the caller can parse. The same serialization as the CLI (`dataclasses.asdict()` for `DebugPacket`, etc.).
- **No interactive prompts.** The MCP server is always non-interactive. `ingest --fix` passes `fix_mode=False`; proposals are suppressed silently. An `--auto-approve` flag is a v2 concern.
- **Lifecycle is external.** The MCP server does not manage the Quine process. Callers that need Quine started should use the CLI `modok quine start` or manage it via launchd. The server pings on each call and surfaces a clear error if Quine is down.

## Tool Surface

```
modok_retrieve(project, source, ticket) -> DebugPacket dict
modok_retrieve_by_node(project, node_id) -> DebugPacket dict
modok_recall(project, feature) -> dict
modok_ingest(project, path) -> IngestionReport dict
```

### `modok_retrieve`

Primary retrieval form. Accepts `source` (source system, e.g. `"zendesk"`) and `ticket` (ticket ID string). Computes the Quine node ID via `idFrom("customer-issue", project, source, ticket)`, calls `retrieve(node_id, project, client)`, and returns the `DebugPacket` as a dict.

**Errors (raises `McpError`):**
- `DRENotFoundError` → `McpError` with `ErrorCode.InvalidRequest` and message "issue not found in project `<slug>`"
- `DREGraphUnavailableError` or `DRELLMUnavailableError` → `McpError` with `ErrorCode.InternalError` and message "infrastructure unavailable: <detail>"
- Unknown project slug → `McpError` with `ErrorCode.InvalidRequest`
- Quine unreachable → `McpError` with `ErrorCode.InternalError`

### `modok_retrieve_by_node`

Power-user form. Accepts `node_id` (int). Calls `retrieve` directly without `idFrom`. Same errors as `modok_retrieve`.

The two retrieve tools are separate MCP tools rather than a single tool with optional arguments because MCP tool schemas are fixed — there is no clean way to express "either (source + ticket) or node_id" in JSON Schema without a union type that most MCP clients cannot validate well.

### `modok_recall`

Returns everything MODOK knows about a feature slug. Runs the same Cypher traversal as the CLI `recall` command. Returns a dict with keys `feature`, `project`, and `nodes`. Empty `nodes` list is a valid success response.

**Errors:** unknown project slug, Quine unreachable (same error mapping as retrieve).

### `modok_ingest`

Runs the ingestion pipeline over `path` for the named project. Always passes `fix_mode=False` (non-interactive). Returns the `IngestionReport` serialized via `dataclasses.asdict(report)`. Does not raise on ingestion errors — the report's `errors` list carries them so the caller can inspect without exception handling.

**Errors:** unknown project slug, Quine unreachable. `RegistryNotFoundError` (raised by `Registry.__init__` when `registries/` is missing or a required file is absent) maps to `McpError` with `ErrorCode.InternalError`.

## Config Loading

Identical to the CLI: reads `~/.modok/config.toml` on every tool call. No global state is mutated between calls. `ModokConfig.load()` from `modok.cli.config` is reused directly.

One new optional section in `config.toml` for the MCP server:

```toml
[mcp]
port = 8181          # HTTP+SSE listen port; default 8181
host = "127.0.0.1"  # bind address; default localhost-only
```

`ModokConfig` gains an optional `mcp: McpConfig` field with these defaults. `--port` and `--host` CLI flags on `modok-mcp` override the config values.

Config errors (`ConfigNotFoundError`, `ConfigParseError`) surface as `McpError` with `ErrorCode.InternalError` so the agent receives a structured error rather than an unhandled exception.

## Error Mapping

| Source | McpError code | Notes |
|---|---|---|
| Config missing / malformed | `InternalError` | Operator error, not caller error |
| Unknown project slug | `InvalidRequest` | Caller supplied bad input |
| Quine unreachable | `InternalError` | Infrastructure error |
| `DRENotFoundError` | `InvalidRequest` | Issue not in graph — caller asked for something that doesn't exist |
| `DREGraphUnavailableError` | `InternalError` | Infrastructure |
| `DRELLMUnavailableError` | `InternalError` | Infrastructure |
| Registry not found | `InternalError` | Operator misconfiguration |

## Transport

Two transports, one implementation. FastMCP supports both from the same tool definitions; the launch mode selects the transport.

### HTTP+SSE (primary — for Codex, Ollama agents, VS Code agents)

A persistent daemon on a configurable port. Non-Claude agents call tools over HTTP using the MCP SSE transport. Launch:

```
modok-mcp --transport sse --port 8181
```

The port defaults to `8181` and is configurable in `~/.modok/config.toml` under `[mcp] port`. The server binds to `127.0.0.1` by default — local only. No auth in v1 (single-user or trusted-team tool, same posture as the rest of MODOK).

Agent config example (OpenAI Codex / VS Code MCP extension):
```json
{
  "mcpServers": {
    "modok": {
      "url": "http://127.0.0.1:8181/sse"
    }
  }
}
```

### stdio (secondary — for Claude Desktop, Claude Code)

The MCP client launches the server as a subprocess. No daemon required; the process lives for the duration of the session.

```
modok-mcp --transport stdio
```

Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "modok": {
      "command": "modok-mcp",
      "args": ["--transport", "stdio"]
    }
  }
}
```

### Entry point

```toml
[project.scripts]
modok-mcp = "modok.mcp.server:main"
```

`main()` parses `--transport` (default `sse`) and `--port` (default `8181`), then calls `mcp.run(transport=..., port=...)`.

### Auth

No auth in v1. The server binds to localhost only. Multi-user or remote access is a future concern — at that point a bearer token or mTLS layer is the right addition, not a shared secret baked into the config.

## Framework

`mcp[cli]` — the official Anthropic Python MCP SDK. `FastMCP` is the high-level decorator-based API:

```python
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("modok")

@mcp.tool()
async def modok_retrieve(project: str, source: str, ticket: str) -> dict:
    ...
```

`FastMCP` handles:
- JSON Schema generation from Python type hints.
- MCP protocol framing (initialization, tool listing, tool call dispatch).
- Error propagation via `mcp.types.McpError`.

Added as a production dependency in `pyproject.toml`: `mcp[cli]>=1.0`.

## Async Model

All four tools are `async def`. FastMCP runs them inside its own async event loop.

- `QuineClient` methods are `async def` — awaited directly inside tools.
- `run_ingestion` is sync. Inside an `async def` tool, it is invoked via `asyncio.to_thread(run_ingestion, ...)` to avoid blocking the event loop. Do **not** use `asyncio.get_event_loop().run_until_complete()` inside an already-running loop — this raises `RuntimeError`.
- `Registry.__init__` is sync and fast (filesystem reads). Called directly without offloading.
- `ModokConfig.load()` is sync and fast. Called directly.

The CLI uses `asyncio.get_event_loop().run_until_complete()` because it runs outside any event loop. The MCP server runs inside one — the patterns are deliberately different.

## Module Layout

```
src/modok/mcp/
    __init__.py
    server.py      # FastMCP instance, tool registrations, main()
    tools/
        __init__.py
        retrieve.py    # modok_retrieve, modok_retrieve_by_node
        recall.py      # modok_recall
        ingest.py      # modok_ingest
    errors.py      # map_error(exc) -> McpError
```

`server.py` imports and registers tools from `tools/`. `errors.py` centralises the exception-to-`McpError` mapping so all four tools use the same translation table.

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|---|---|---|---|
| MCP framework | `FastMCP` (official `mcp[cli]` SDK) | `mcp` low-level server, hand-rolled JSON-RPC | `FastMCP` generates JSON Schema from type hints automatically, handles protocol boilerplate. Low-level API offers more control but requires manual schema declaration — unnecessary for four simple tools. |
| Transport | Both HTTP+SSE and stdio via `--transport` flag | SSE only; stdio only | Primary callers (Codex, Ollama agents) need HTTP. Claude Desktop needs stdio. FastMCP supports both from one implementation — no reason to choose. SSE is the default because it serves the wider caller population. |
| Two retrieve tools vs one | Two separate tools (`modok_retrieve`, `modok_retrieve_by_node`) | Single tool with optional args | MCP JSON Schema cannot cleanly express mutual exclusion between arg groups. Two tools have unambiguous schemas; agents can choose the right one without schema gymnastics. |
| `fix_mode` in `modok_ingest` | Always `False` | Respect a `fix` bool arg | The MCP server is always non-interactive. There is no stderr prompt channel. Accepting a `fix=True` arg that silently does nothing would be confusing. |
| Config loading | Reuse `ModokConfig.load()` from `modok.cli.config` | Duplicate config logic in mcp module | Single config path, single parse, single pydantic model. No divergence. |
| Error surface | `McpError` with typed `ErrorCode` | Return error in result dict | `McpError` is the MCP-native error type. Agents receive structured errors they can inspect without parsing text output. |
| Lifecycle (Quine mgmt) | No lifecycle management in MCP server | Wrap `modok quine start` as a tool | The MCP server is a query/ingest interface, not a process manager. Operators start Quine via CLI or launchd. Adding process management here blurs responsibilities. |
| `run_ingestion` in async context | `asyncio.to_thread()` | Direct call, `run_until_complete` | FastMCP runs tools inside an event loop. Direct sync call risks blocking the loop under large repos. `run_until_complete` inside a running loop raises `RuntimeError`. `to_thread` is the correct pattern. |
| `node_id` type in `modok_retrieve_by_node` | Python `int` (JSON `integer`) | `str` with parsing | FastMCP maps `int` → JSON Schema `integer`. Most MCP clients send integers for integer-typed args. If a client sends a string, FastMCP raises a schema validation error before the tool runs — not a silent bug. |

## Open Questions & Future Decisions

### Deferred

1. **Remote / multi-machine access** — the server currently binds to `127.0.0.1` only. A multi-machine setup (Mac mini serving multiple laptops) requires either a reverse proxy or binding to `0.0.0.0` with a bearer token. Defer until someone needs it.
2. **`modok_init` tool** — initializes a project from an agent. Omitted in v1 because init writes to the filesystem (git hook, registry stubs, config) which is outside the scope of what an agent tool should do silently. Revisit if there is a clear agent-driven onboarding use case.
3. **`auto_approve` for `modok_ingest`** — an `auto_approve=True` arg that enables LLM fix proposals with no human gate. Defer until there is a clear CI/agent use case.
4. **Resource exposure** — MCP supports `resources` (static or dynamic data the server can serve). MODOK could expose graph nodes as MCP resources by node ID. Defer until a client uses resources rather than tool calls.
5. **Project selection from MCP client config** — some MCP integrations pass per-server config. If a Claude Desktop user has only one project, having to pass `project="stagehand"` on every call is friction. An optional default project in `~/.modok/config.toml` could eliminate it. Defer until user reports the friction.

## References

- `docs/high-level-design.md` — CLI / MCP component description
- `docs/llds/cli.md` — CLI LLD (parallel surface, same core)
- `docs/llds/diagnostic-retrieval-engine.md` — `retrieve` entry point
- `docs/llds/ingestion-pipeline.md` — `run_ingestion` entry point
- `docs/llds/quine-client.md` — `QuineClient`, `ping()`
- MCP Python SDK: https://github.com/anthropics/python-sdk-mcp
