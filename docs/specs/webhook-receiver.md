# Webhook Receiver Specs

Specs for `modok.webhook` — the HTTP webhook receiver that routes push events and pull-adapter events into the existing ingestion pipeline.

LLD: `docs/llds/webhook-receiver.md`

---

## Test Level Convention

See `docs/testing-standard.md` for full definitions.

- **[U]** — Unit test with mocked dependencies.
- **[P]** — Property test (`hypothesis`). Implies [U].
- **[C]** — Contract test against live Quine instance. Implies [U].

---

## `modok serve` Startup

- [x] **WH-SERVE-001** [U]: When `modok serve` is invoked, the system shall load `~/.modok/config.toml` and exit `1` with a config error message if the file is missing or invalid.
- [x] **WH-SERVE-002** [U]: When `modok serve` is invoked and `github` is in `enabled_sources` but `github_secret` is not set in `[webhook]` config, the system shall exit `1` with the message "github_secret not configured — set [webhook] github_secret in config".
- [x] **WH-SERVE-003** [U]: When `modok serve` is invoked and `ticket` is in `enabled_sources` but `bearer_token` is not set in `[webhook]` config, the system shall exit `1` with the message "bearer_token not configured — set [webhook] bearer_token in config".
- [x] **WH-SERVE-007** [U]: The `[webhook]` config shall accept an `enabled_sources` list (e.g. `["github", "ticket"]`). When `enabled_sources` is absent, all implemented push adapters are enabled. When present, only listed sources are active; requests to unlisted source paths shall return HTTP 404 and their secrets shall not be required at startup.
- [x] **WH-SERVE-004** [U]: When `modok serve` is invoked and `QuineClient.ping()` returns `False`, the system shall exit `2` with the message "Quine is not reachable at `<url>` — run `modok quine start` or check your config".
- [x] **WH-SERVE-005** [U]: When `modok serve` starts successfully, the system shall log the bound address and port to stderr.
- [x] **WH-SERVE-006** [U]: `modok serve` shall bind to `127.0.0.1` by default. The `--host` flag shall override the bind address and the `--port` flag shall override the port (default `4242`).

---

## Routing

- [ ] **WH-ROUTE-001** [U]: When a `POST /webhook/{project_slug}/{source}` request arrives with an unknown `project_slug`, the system shall return HTTP 404.
- [ ] **WH-ROUTE-002** [U]: When a `POST /webhook/{project_slug}/{source}` request arrives with an unknown `source` (no matching entry in `PUSH_ADAPTERS`, or the source is not in `enabled_sources`), the system shall return HTTP 404.
- [ ] **WH-ROUTE-003** [U]: When `GET /health` is requested, the system shall return HTTP 200 with a JSON body containing `{"status": "ok", "quine": true|false}`. The `quine` field shall reflect the result of the most recent periodic ping (updated every 30 seconds), not a live ping per request.
- [ ] **WH-ROUTE-004** [U]: When the ingestion pipeline completes successfully and the cached Quine status was `false`, the system shall update the cached status to `true`.

---

## Push Adapter Protocol

- [ ] **WH-PUSH-001** [U]: Each push adapter shall implement `verify_request(request: Request, config: WebhookConfig) -> None`, raising `WebhookAuthError` if the request cannot be verified.
- [ ] **WH-PUSH-002** [U]: Each push adapter shall implement `normalize_event(payload: dict, event_type: str) -> IngestEvent | None`, returning `None` to indicate the event should be silently skipped.
- [ ] **WH-PUSH-003** [U]: When `normalize_event` returns an `IngestEvent` with `kind="skip"`, the system shall return HTTP 200 with `{"status": "skipped"}` and perform no graph write.
- [ ] **WH-PUSH-004** [U]: When `verify_request` raises `WebhookAuthError`, the system shall return HTTP 401 and perform no graph write.
- [ ] **WH-PUSH-005** [U]: When the request body is not valid JSON, the system shall return HTTP 400 and perform no graph write.
- [ ] **WH-PUSH-006** [U]: When the ingestion pipeline raises any exception (including Quine unreachable), the system shall return HTTP 500 with `{"status": "error", "detail": "<message>"}`.
- [ ] **WH-PUSH-007** [U]: When the ingestion pipeline completes successfully, the system shall return HTTP 200 with `{"status": "ok", "nodes_written": N}`.
- [ ] **WH-PUSH-008** [U]: The system shall invoke the ingestion pipeline via `asyncio.to_thread` so that pipeline execution does not block the event loop.
- [ ] **WH-PUSH-009** [U]: The system shall read the raw request body bytes before JSON parsing. `verify_request` shall be called with the raw bytes available; JSON parsing shall occur after verification completes.

---

## Pull Adapter Protocol

- [ ] **WH-PULL-001** [U]: Each pull adapter shall implement `async start(config: WebhookConfig, on_event: Callable[[IngestEvent], Awaitable[None]]) -> None`, connecting to its source and invoking `on_event(event)` for each received event.
- [ ] **WH-PULL-002** [U]: Each pull adapter shall implement `async stop() -> None`, gracefully disconnecting from its source.
- [ ] **WH-PULL-003** [U]: The system shall call `start()` on all configured pull adapters during `modok serve` startup, after the Quine ping succeeds.
- [ ] **WH-PULL-004** [U]: The system shall call `stop()` on all running pull adapters when `modok serve` receives a shutdown signal (SIGINT or SIGTERM).
- [ ] **WH-PULL-005** [U]: The `on_event` callback passed to pull adapter `start()` shall be an async wrapper around `run_ingest_event` (implemented via `asyncio.to_thread`), so pull adapters can `await on_event(event)` without blocking the event loop. Pull adapters shall not call `asyncio.to_thread` directly.

---

## GitHub Adapter

- [ ] **WH-GH-001** [U]: When a `POST /webhook/{slug}/github` request arrives, the system shall compute `HMAC-SHA256(body_bytes, github_secret)` and compare it to the `X-Hub-Signature-256` header using `hmac.compare_digest`. A missing header or digest mismatch shall raise `WebhookAuthError`.
- [ ] **WH-GH-002** [U]: When the `X-GitHub-Event` header is `ping`, the system shall verify the HMAC, return HTTP 200 with `{"status": "skipped"}`, and perform no graph write.
- [ ] **WH-GH-003** [U]: When the `X-GitHub-Event` is `issues` and `action` is `opened`, `edited`, `labeled`, `closed`, or `reopened`, the system shall produce a `CustomerIssue` `IngestEvent`.
- [ ] **WH-GH-004** [U]: When the `X-GitHub-Event` is `pull_request`, `action` is `closed`, and `merged` is `true`, the system shall produce a `Fix` `IngestEvent`.
- [ ] **WH-GH-005** [U]: When the `X-GitHub-Event` is `pull_request` with any `action` other than a merged close, the system shall produce `kind="skip"`.
- [ ] **WH-GH-006** [U]: When the `X-GitHub-Event` header is any value other than `issues`, `pull_request`, or `ping`, the system shall produce `kind="skip"`.
- [ ] **WH-GH-007** [U]: The GitHub adapter shall map `issue.number` to `ticket_id` (as a string), `issue.title` to `summary`, `issue.body` (or empty string if null) to `raw_text`, and `"open"`/`"closed"` to `status`.
- [ ] **WH-GH-008** [U]: The GitHub adapter shall map `"gh-" + str(pr.number)` to `fix_id` and `pr.title` to `summary` for `Fix` events.
- [ ] **WH-GH-009** [P]: For any valid GitHub `issues` or `pull_request` webhook payload, running the GitHub adapter twice shall produce the same `IngestEvent` (normalization is deterministic).
- [x] **WH-GH-010** [U]: The GitHub adapter shall derive `ticket_kind` from `issue.labels` using the same `ticket_kind_from_labels` function `GithubIngester` uses (`docs/specs/github-ingestion.md § GHING-ISSUE-003`), so a ticket's `ticket_kind` is identical regardless of whether it arrived via webhook push or GitHub poll.

---

## Generic Ticket Adapter

- [ ] **WH-TKT-001** [U]: When a `POST /webhook/{slug}/ticket` request arrives, the system shall verify the `Authorization: Bearer <token>` header against `bearer_token` in config. A missing or non-matching token shall raise `WebhookAuthError`.
- [ ] **WH-TKT-002** [U]: The generic ticket adapter shall accept a JSON body with required fields `ticket_id` and `summary`, and optional fields `body` (default `""`) and `source_system` (default `"webhook"`). Unknown fields shall be silently ignored.
- [ ] **WH-TKT-003** [U]: When required fields `ticket_id` or `summary` are absent from the payload, the system shall return HTTP 400.
- [ ] **WH-TKT-004** [P]: For any valid generic ticket payload, running the generic ticket adapter twice shall produce the same `IngestEvent`.

---

## Idempotency

- [ ] **WH-IDEM-001** [P]: Delivering the same event twice to `POST /webhook/{project_slug}/{source}` (identical payload and headers) shall produce no duplicate nodes or edges in Quine. The second delivery shall be a no-op upsert. (The standing-query result route, `POST /standing-query/result`, has its own dedicated idempotency requirement — `SQ-INV-005` in `docs/specs/standing-queries.md` — since its dedup key is a computed `investigation_id`, not a re-delivered payload.)
- [ ] **WH-IDEM-002** [U]: When Quine is unreachable during event processing, the system shall return HTTP 500. GitHub's automatic retry shall re-deliver the event; on the retry the pipeline shall write the nodes as if it were the first delivery.
- [ ] **WH-IDEM-003** [U]: The system shall write `CustomerIssue` and `Fix` nodes via the same upsert path used by `ingest-github`, keyed on `idFrom("CustomerIssue" | "Fix", project_slug, ...)`. Insert-only writes are not permitted.

---

## Adapter Extension Invariants

- [ ] **WH-EXT-001** [U]: A push adapter registered in `PUSH_ADAPTERS` with key `"test-source"` shall be reachable at `POST /webhook/test-project/test-source` and shall receive dispatched requests without any other configuration change.
- [ ] **WH-EXT-002** [U]: A pull adapter registered in `PULL_ADAPTERS` with key `"test-pull"` shall have `start()` called during server startup and `stop()` called on shutdown without any other configuration change.
- [ ] **WH-EXT-003** [U]: An `IngestEvent` with `kind="customer_issue"` or `kind="fix"` produced by any adapter shall be accepted by `run_ingest_event` and result in a graph write, with no branching in `run_ingest_event` on adapter identity or source system.
- [x] **WH-EXT-004** [U]: An `IngestEvent` with `kind="investigation"` — constructed by the dedicated `POST /standing-query/result` route (`docs/specs/standing-queries.md § SQ-ROUTE`) rather than by a `PUSH_ADAPTERS`/`PULL_ADAPTERS` entry — shall likewise be accepted by `run_ingest_event` and result in a graph write, with no branching in `run_ingest_event` on the event's origin.
