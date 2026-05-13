from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from modok.quine.client import QuineClient
from modok.quine.models import CustomerIssue, Fix
from modok.webhook.errors import WebhookAuthError
from modok.webhook.models import CustomerIssueData, FixData, IngestEvent, WebhookConfig
from modok.webhook.router import PULL_ADAPTERS, PUSH_ADAPTERS


# ---------------------------------------------------------------------------
# Pipeline entry point
# @spec WH-IDEM-003, WH-EXT-003
# ---------------------------------------------------------------------------


def run_ingest_event(event: IngestEvent, quine_client: Any) -> int:
    """Write an IngestEvent to Quine. Returns number of nodes written.

    Sync — called via asyncio.to_thread from the async route handler.
    Routes purely on event.kind; no branching on adapter identity or source_system.
    """
    # @spec WH-IDEM-003, WH-EXT-003
    if event.kind == "customer_issue":
        assert isinstance(event.data, CustomerIssueData)
        node = CustomerIssue(
            node_type="CustomerIssue",
            project_slug=event.project_slug,
            source_system=event.data.source_system,
            ticket_id=event.data.ticket_id,
            summary=event.data.summary,
            raw_text=event.data.raw_text,
            status=event.data.status,
        )
        asyncio.run(quine_client.upsert_node(node))
        return 1

    if event.kind == "fix":
        assert isinstance(event.data, FixData)
        node = Fix(
            node_type="Fix",
            project_slug=event.project_slug,
            fix_id=event.data.fix_id,
            summary=event.data.summary,
            kind=event.data.kind,
        )
        asyncio.run(quine_client.upsert_node(node))
        return 1

    return 0


# ---------------------------------------------------------------------------
# Pull adapter lifecycle
# @spec WH-PULL-003, WH-PULL-004, WH-PULL-005
# ---------------------------------------------------------------------------


async def _make_on_event(quine_client: Any) -> Callable[[IngestEvent], Awaitable[None]]:
    async def on_event(event: IngestEvent) -> None:
        await asyncio.to_thread(run_ingest_event, event, quine_client)
    return on_event


async def start_pull_adapters(
    adapters: dict[str, Any],
    config: WebhookConfig,
    quine_client: Any,
) -> None:
    # @spec WH-PULL-003, WH-PULL-005
    on_event = await _make_on_event(quine_client)
    for adapter in adapters.values():
        await adapter.start(config, on_event)


async def stop_pull_adapters(adapters: dict[str, Any]) -> None:
    # @spec WH-PULL-004
    for adapter in adapters.values():
        await adapter.stop()


# ---------------------------------------------------------------------------
# Config validation
# @spec WH-SERVE-002, WH-SERVE-003, WH-SERVE-007
# ---------------------------------------------------------------------------


def validate_config(config: WebhookConfig) -> None:
    active = config.enabled_sources if config.enabled_sources is not None else list(PUSH_ADAPTERS.keys())
    if "github" in active and not config.github_secret:
        print(
            "github_secret not configured — set [webhook] github_secret in config",
            file=sys.stderr,
        )
        sys.exit(1)
    if "ticket" in active and not config.bearer_token:
        print(
            "bearer_token not configured — set [webhook] bearer_token in config",
            file=sys.stderr,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Quine startup check
# @spec WH-SERVE-004
# ---------------------------------------------------------------------------


async def check_quine(quine_client: Any, config: WebhookConfig) -> None:
    reachable = await quine_client.ping()
    if not reachable:
        url = getattr(quine_client, "_base_url", "<unknown>")
        print(
            f"Quine is not reachable at {url} — run `modok quine start` or check your config",
            file=sys.stderr,
        )
        sys.exit(2)


# ---------------------------------------------------------------------------
# App factory
# @spec WH-SERVE-005, WH-SERVE-006, WH-ROUTE-001, WH-ROUTE-002, WH-ROUTE-003,
#        WH-ROUTE-004, WH-PUSH-003..009, WH-EXT-001, WH-EXT-002
# ---------------------------------------------------------------------------


def build_app(
    *,
    config: WebhookConfig,
    quine_client: Any,
    host: str = "127.0.0.1",
    port: int = 4242,
    extra_push_adapters: dict[str, Any] | None = None,
    extra_pull_adapters: dict[str, Any] | None = None,
    known_project_slugs: set[str] | None = None,
) -> FastAPI:
    push_adapters = {**PUSH_ADAPTERS, **(extra_push_adapters or {})}
    pull_adapters = {**PULL_ADAPTERS, **(extra_pull_adapters or {})}

    active_sources = (
        set(config.enabled_sources)
        if config.enabled_sources is not None
        else set(push_adapters.keys())
    )

    # @spec WH-SERVE-005 — emit startup message when app object is created so
    # build_app() callers (including tests using capsys) observe it immediately.
    print(f"modok webhook receiver listening on {host}:{port}", file=sys.stderr)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.quine_healthy = True
        await start_pull_adapters(pull_adapters, config, quine_client)
        yield
        await stop_pull_adapters(pull_adapters)

    app = FastAPI(lifespan=lifespan)
    app.state.host = host
    app.state.port = port
    app.state.quine_healthy = True
    app.state.known_project_slugs = known_project_slugs

    # ---------------------------------------------------------------------------
    # /health
    # @spec WH-ROUTE-003
    # ---------------------------------------------------------------------------

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "quine": app.state.quine_healthy}

    # ---------------------------------------------------------------------------
    # POST /webhook/{project_slug}/{source}
    # @spec WH-ROUTE-001, WH-ROUTE-002, WH-PUSH-003..009
    # ---------------------------------------------------------------------------

    @app.post("/webhook/{project_slug}/{source}")
    async def handle_webhook(
        project_slug: str, source: str, request: Request
    ) -> Response:
        # WH-ROUTE-001: unknown project slug → 404
        slugs = app.state.known_project_slugs
        if slugs is not None and project_slug not in slugs:
            return JSONResponse({"detail": f"Unknown project: {project_slug}"}, status_code=404)

        # WH-ROUTE-002: unknown or disabled source → 404
        if source not in push_adapters or source not in active_sources:
            return JSONResponse({"detail": f"Unknown source: {source}"}, status_code=404)

        adapter = push_adapters[source]

        # WH-PUSH-009: read raw bytes BEFORE JSON parsing
        body_bytes = await request.body()

        # Attach raw bytes to a thin request wrapper for adapters
        class _RequestWrapper:
            def __init__(self, req: Request, raw: bytes) -> None:
                self.headers = dict(req.headers)
                self.body_bytes = raw

        wrapped = _RequestWrapper(request, body_bytes)

        # WH-PUSH-004: verify auth
        try:
            adapter.verify_request(wrapped, config)
        except WebhookAuthError:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)

        # WH-PUSH-005: parse JSON
        try:
            payload: dict = json.loads(body_bytes)
        except (json.JSONDecodeError, ValueError):
            return JSONResponse({"detail": "Invalid JSON"}, status_code=400)

        # Validate required fields for ticket adapter (pydantic raises ValidationError)
        if source == "ticket":
            from modok.webhook.adapters.ticket import _TicketPayload
            try:
                _TicketPayload.model_validate(payload)
            except ValidationError:
                return JSONResponse({"detail": "Missing required fields"}, status_code=400)

        event_type = request.headers.get("X-GitHub-Event", source)

        # Attach project slug for adapters to read
        payload["__project_slug__"] = project_slug

        # WH-PUSH-003: normalize — None or skip → 200 skipped
        try:
            event = adapter.normalize_event(payload, event_type)
        except Exception as exc:
            return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)

        if event is None or event.kind == "skip":
            return JSONResponse({"status": "skipped"})

        # Stamp project slug from path (authoritative over payload)
        event = IngestEvent(kind=event.kind, project_slug=project_slug, data=event.data)

        # WH-PUSH-008: run pipeline in thread pool
        try:
            nodes_written = await asyncio.to_thread(run_ingest_event, event, quine_client)
        except Exception as exc:
            return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)

        # WH-ROUTE-004: update cached Quine status on success
        app.state.quine_healthy = True

        # WH-PUSH-007
        return JSONResponse({"status": "ok", "nodes_written": nodes_written})

    return app


# ---------------------------------------------------------------------------
# Config loading (thin wrapper over ModokConfig for testability)
# ---------------------------------------------------------------------------


def load_config() -> WebhookConfig:
    from modok.cli.config import ModokConfig
    modok_cfg = ModokConfig.load()
    raw = getattr(modok_cfg, "_raw", {})
    webhook_raw = raw.get("webhook", {})
    return WebhookConfig.model_validate(webhook_raw)


# ---------------------------------------------------------------------------
# CLI entry point
# @spec WH-SERVE-001..007
# ---------------------------------------------------------------------------


def serve_main(args: list[str]) -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="modok serve")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4242)
    parsed = parser.parse_args(args)

    try:
        config = load_config()
    except Exception as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(1)

    validate_config(config)

    quine_client = _make_quine_client(config)
    asyncio.get_event_loop().run_until_complete(check_quine(quine_client, config))

    app = build_app(
        config=config,
        quine_client=quine_client,
        host=parsed.host,
        port=parsed.port,
    )

    import uvicorn
    uvicorn.run(app, host=parsed.host, port=parsed.port)


def _make_quine_client(config: WebhookConfig) -> QuineClient:
    from modok.cli.config import ModokConfig
    modok_cfg = ModokConfig.load()
    return QuineClient(base_url=modok_cfg.quine.url)
