# LLM Gateway Specs

Specs for `modok.llm` — the abstract LLM boundary that returns proposals for ticket parsing, metadata suggestion, and similarity matching.

LLD: `docs/llds/llm-gateway.md`

---

## Test Level Convention

See `docs/testing-standard.md` for full definitions.

- **[U]** — Unit test with mocked dependencies.
- **[P]** — Property test (`hypothesis`). Implies [U].
- **[C]** — Contract test against live Quine instance. Implies [U].

---

## Backend Selection

- [ ] **LLM-BACK-001** [U]: When `backend="local"`, the system shall send the request to the configured `local_endpoint` using the configured `local_model`, regardless of whether a remote backend is configured.
- [ ] **LLM-BACK-002** [U]: When `backend="remote"` and no remote endpoint or API key is configured, the system shall raise `LLMConfigError` without making any network call.
- [ ] **LLM-BACK-003** [U]: When `backend="auto"`, the system shall attempt the local backend first. If the local response fails pydantic validation, the system shall escalate to the remote backend if configured. Escalation happens at most once per gateway call.
- [ ] **LLM-BACK-005** [U]: When `backend="auto"` and no remote backend is configured, the system shall behave identically to `backend="local"` and shall not raise an error due to the absence of a remote backend.
- [ ] **LLM-BACK-006** [U]: The API key shall be read from `remote_api_key` in config first; if absent or empty, from the `MODOK_LLM_API_KEY` environment variable. If neither is set and a remote call is attempted, the system shall raise `LLMConfigError`.

---

## Retry and Timeout

- [ ] **LLM-RETRY-001** [U]: On a timeout or 5xx response, the system shall retry up to `max_retries` times with a 1-second fixed delay between attempts, on the same backend. Retry does not trigger backend escalation — escalation is triggered only by validation failure (LLM-BACK-003).
- [ ] **LLM-RETRY-002** [U]: On a 4xx response, the system shall raise `LLMGatewayError` immediately without retrying.
- [ ] **LLM-RETRY-003** [U]: After all retries are exhausted without a valid response, the system shall raise `LLMUnavailableError`.
- [ ] **LLM-RETRY-004** [U]: `parse_ticket` calls shall use `timeout_parse_ticket` (default 30s) per attempt. `propose_metadata` and `propose_similarity` calls shall use `timeout_propose_metadata` and `timeout_propose_similarity` respectively (default 15s each). When a per-call-type timeout key is absent from config, the system shall fall back to `timeout_seconds`.
- [ ] **LLM-RETRY-005** [P]: The total number of network attempts for any single gateway call shall never exceed `max_retries + 1`.

---

## Structured Output and Validation

- [ ] **LLM-VAL-001** [U]: All gateway calls shall set `response_format: {"type": "json_object"}` in the request.
- [ ] **LLM-VAL-002** [U]: When the model response is valid JSON matching the expected pydantic schema, the system shall return the typed result without error.
- [ ] **LLM-VAL-003** [U]: When the model response is not valid JSON but contains an extractable JSON object in the raw text, the system shall extract and validate it. A successful extraction counts as a valid response — no retry is triggered and no error is raised. A failed extraction does not consume a retry attempt; the LLM call is retried instead.
- [ ] **LLM-VAL-004** [U]: When the model response cannot be parsed as JSON after extraction, the system shall raise `LLMResponseError` after all retries are exhausted.
- [ ] **LLM-VAL-005** [U]: When the model response is valid JSON but fails pydantic schema validation, the system shall raise `LLMResponseError` after all retries are exhausted.
- [ ] **LLM-VAL-006** [U]: `LLMResponseError` shall always propagate to the caller; the gateway shall never swallow it or return a partial result in its place.

---

## Ticket Parsing

- [ ] **LLM-TICKET-001** [U]: `parse_ticket` shall send the raw ticket text and project slug to the LLM and return a `TicketParseResult` containing `feature_slug`, `error_signatures`, `environment`, `symptoms`, `confidence`, and `raw_response`.
- [ ] **LLM-TICKET-002** [U]: When the model does not report a confidence value, `TicketParseResult.confidence` shall default to `0.0`.
- [ ] **LLM-TICKET-003** [U]: `parse_ticket` shall use the frozen system prompt template from `modok.llm.prompts` and shall not construct prompts dynamically beyond interpolating `project_slug` into the template.
- [ ] **LLM-TICKET-004** [U]: `parse_ticket` shall never write to Quine or to any file; it returns a result struct only.

---

## Metadata Proposal

- [ ] **LLM-META-001** [U]: `propose_metadata` shall send the doc path, current frontmatter, and list of missing field names to the LLM and return a `MetadataProposal` containing `proposed_fields`, `confidence`, `evidence`, and `raw_response`.
- [ ] **LLM-META-002** [U]: `propose_metadata` shall use the frozen system prompt template from `modok.llm.prompts`.
- [ ] **LLM-META-003** [U]: `propose_metadata` shall never write to Quine or to any file; the caller (`apply_llm_proposals` in the ingestion pipeline) owns all writes.
- [ ] **LLM-META-004** [U]: When `propose_metadata` raises `LLMResponseError` or `LLMUnavailableError`, the ingestion pipeline shall catch the exception, emit a structured warning, and skip writing for that doc — it shall not halt ingestion of other files.

---

## Similarity Proposal

- [ ] **LLM-SIM-001** [U]: `propose_similarity` shall accept a `CustomerIssue` and a list of `KnownIssueSummary` structs and return a list of `SimilarityProposal` structs; it shall not query Quine.
- [ ] **LLM-SIM-002** [U]: Each `SimilarityProposal` shall contain `known_issue_id`, `score`, `method` (always `"llm"`), `evidence_anchors`, and `raw_response`.
- [ ] **LLM-SIM-003** [U]: When `candidates` is an empty list, `propose_similarity` shall return an empty list without making any LLM call, regardless of the `backend` parameter value.
- [ ] **LLM-SIM-004** [U]: `propose_similarity` shall use the frozen system prompt template from `modok.llm.prompts`.
- [ ] **LLM-SIM-005** [U]: `propose_similarity` shall never write to Quine or to any file.

---

## Gateway Write Boundary

- [ ] **LLM-WRITE-001** [U]: No gateway function shall call `upsert_node`, `write_edge`, or any Quine client method.
- [ ] **LLM-WRITE-002** [U]: No gateway function shall write to any file on disk.
- [ ] **LLM-WRITE-003** [P]: The `raw_response` field in any result struct shall contain the raw model output string and shall not be persisted by the gateway; it is returned in-memory to the caller only.

---

## Prompt Discipline

- [ ] **LLM-PROMPT-001** [U]: All prompt templates shall be defined as module-level string constants in `modok.llm.prompts`; no prompt text shall be constructed at runtime outside of field interpolation.
- [ ] **LLM-PROMPT-002** [U]: Each of the three call types (`parse_ticket`, `propose_metadata`, `propose_similarity`) shall use a distinct system prompt template.
