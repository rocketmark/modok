# LLM Gateway

## Context and Design Philosophy

The LLM Gateway is MODOK's controlled boundary between mechanical graph operations and language model inference. Every LLM call in the system routes through this component.

Three uses exist in v1:

1. **Ticket parsing** — convert freeform customer issue text into structured YAML (feature slug, error signatures, environment, symptoms).
2. **Metadata proposal** — suggest missing frontmatter fields for a doc when `--fix` is specified.
3. **Similarity proposal** — given a new CustomerIssue, suggest candidate KnownIssue matches with evidence anchors.

One rule governs all three: **the gateway returns proposals, never writes**. No gateway output touches Quine or doc files directly; the caller owns the write decision.

## Interface

The gateway exposes a single async function per use case:

```python
async def parse_ticket(raw_text: str, project_slug: str) -> TicketParseResult
async def propose_metadata(doc_path: Path, frontmatter: dict, missing_fields: list[str]) -> MetadataProposal
async def propose_similarity(issue: CustomerIssue, project_slug: str) -> list[SimilarityProposal]
```

All three are thin wrappers around `_chat_completion(messages, response_format)`, which handles backend selection, retry, and timeout.

## Backend Model

The gateway is backend-agnostic. It communicates via the OpenAI-compatible chat completions endpoint (`POST /v1/chat/completions`) with structured JSON output (`response_format: {type: "json_object"}`). Both Ollama and Claude/GPT-4 support this interface.

### Backend selection

```
local  →  Ollama (http://localhost:11434/v1)        default
remote →  configured provider endpoint + api_key    optional escalation
```

Backend is selected per-call by the `backend` parameter: `"local"` (default), `"remote"`, or `"auto"`.

`"auto"` runs local first. If the local response fails validation or returns confidence below `auto_escalation_threshold` (default 0.60), it escalates to remote. Remote is only attempted if configured; if not configured, `auto` behaves as `local`.

### Configuration (from `~/.modok/config.toml`)

```toml
[llm]
local_endpoint  = "http://localhost:11434/v1"
local_model     = "llama3.2"
remote_endpoint = "https://api.anthropic.com/v1"   # optional
remote_model    = "claude-sonnet-4-6"               # optional
remote_api_key  = ""                                # optional; read from env if absent
auto_escalation_threshold = 0.60
timeout_seconds = 30
max_retries     = 2
```

API key is read from `remote_api_key` in config first, then from the environment variable `MODOK_LLM_API_KEY`. If neither is set and a remote call is attempted, the gateway raises `LLMConfigError`.

## Retry and Timeout

- Each attempt has a fixed timeout of `timeout_seconds` (default 30s).
- On timeout or 5xx response: retry up to `max_retries` times with 1s fixed delay (no exponential — LLM calls are already slow; jitter adds little).
- On 4xx (auth, rate limit): raise immediately without retry — these are caller errors, not transient failures.
- After all retries exhausted: raise `LLMUnavailableError`.

## Structured Output

All three call types use `response_format: {"type": "json_object"}` to constrain output. The gateway validates the response against a pydantic model before returning. If validation fails after all retries, raises `LLMResponseError`.

### Prompt discipline

Each call type has a fixed system prompt template stored in `modok/llm/prompts.py`. Templates are frozen strings — no runtime prompt construction beyond interpolating the specific values (raw text, missing field names, etc.). This keeps the gateway testable and auditable.

## Response Types

### `TicketParseResult`

```python
@dataclass
class TicketParseResult:
    feature_slug: str | None
    error_signatures: list[str]
    environment: dict[str, str]
    symptoms: list[str]
    confidence: float          # 0.0–1.0; model self-reported
    raw_response: str          # for audit/debug
```

### `MetadataProposal`

```python
@dataclass
class MetadataProposal:
    proposed_fields: dict[str, Any]   # field_name → proposed_value
    confidence: float
    evidence: str                     # one-sentence rationale
    raw_response: str
```

### `SimilarityProposal`

```python
@dataclass
class SimilarityProposal:
    known_issue_id: str
    score: float
    method: str          # always "llm" from this gateway
    evidence_anchors: list[str]
    raw_response: str
```

## Error Types

```python
class LLMGatewayError(Exception): pass
class LLMConfigError(LLMGatewayError): pass       # missing/invalid config
class LLMUnavailableError(LLMGatewayError): pass  # all retries exhausted
class LLMResponseError(LLMGatewayError): pass     # response failed validation
```

## ID Scheme

The LLM Gateway writes no nodes — it has no Quine ID concerns. Callers own node creation.

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|---|---|---|---|
| OpenAI-compatible endpoint | Single interface for all backends | Separate Ollama SDK + Anthropic SDK | One code path; Ollama, Claude, GPT-4, and any future model support it; no provider lock-in |
| Local-first with optional escalation | Ollama default; remote opt-in | Remote-first; always-local; always-remote | Local keeps costs zero for normal use; remote available when local model is insufficient |
| `auto` escalation threshold | 0.60 confidence (configurable) | Hard-coded; always escalate; never escalate | Threshold captures the "local model clearly uncertain" case without always paying remote cost |
| Fixed 1s retry delay | Simple fixed delay | Exponential backoff | LLM calls are already 5–30s; backoff adds negligible benefit and complicates reasoning |
| API key from env fallback | `MODOK_LLM_API_KEY` env var | Config file only; keychain | Env var is the standard CI/server pattern; config file is for local dev; both supported |
| `response_format: json_object` | JSON mode on all calls | Free-form text parsed with regex; function calling / tool use | JSON mode is the most portable structured output across all providers; function calling is provider-specific |
| Prompts in `prompts.py` | Fixed frozen templates | Loaded from YAML/TOML at runtime; user-configurable | Fixed templates are auditable and testable; runtime loading adds attack surface and complexity |
| Gateway never writes | Proposals returned to caller | Gateway writes directly to Quine; gateway writes to doc file | Keeps write path mechanical; gateway is purely read/inference; audit trail stays in caller |

## Open Questions & Future Decisions

### Resolved
1. ✅ Backend protocol — OpenAI-compatible chat completions endpoint for all backends.
2. ✅ Backend selection — local-first, remote as opt-in escalation, `auto` mode with configurable threshold.
3. ✅ API key source — config file first, env var fallback, error if remote attempted without key.
4. ✅ Retry strategy — fixed 1s delay, `max_retries` attempts, immediate raise on 4xx.
5. ✅ Structured output — `json_object` response format, pydantic validation, `LLMResponseError` on failure.
6. ✅ Write responsibility — gateway returns proposals; callers own all writes.

### Deferred
1. **Streaming responses** — currently uses non-streaming completions. Streaming would improve perceived latency for long similarity proposals but adds response assembly complexity. Deferred until a concrete UX need arises.
2. **Prompt versioning** — prompts in `prompts.py` are frozen strings. If prompt tuning becomes a workflow, a versioning scheme (hash in log, stored alongside response) would help audit which prompt produced which result. Not needed at current scale.
3. **Local model selection per call type** — currently one `local_model` for all three call types. Ticket parsing may benefit from a smaller/faster model than similarity proposals. Deferred until model diversity in the local stack justifies it.

## References

- `docs/llds/static-ingestion.md` — primary caller for metadata proposal
- `docs/llds/quine-client.md` — write primitives (gateway does not use directly)
- `docs/high-level-design.md §2` — LLM-agnostic gateway decision
