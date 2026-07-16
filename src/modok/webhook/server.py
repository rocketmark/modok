from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from modok.ingestion.anchor_linking import (
    classify_customer_issue_anchors,
    link_customer_issue_error_anchors,
    link_customer_issue_feature_anchors,
)
from modok.ingestion.github import post_issue_comment
from modok.quine.client import QuineClient
from modok.quine.models import CustomerIssue, Investigation, InvestigationMilestone
from modok.webhook.errors import WebhookAuthError
from modok.webhook.models import (
    IngestEvent,
    InvestigationData,
    MilestoneData,
    WebhookConfig,
)
from modok.webhook.pipeline import run_ingest_event
from modok.webhook.router import PULL_ADAPTERS, PUSH_ADAPTERS

_UNCONFIGURED_REPO_ROOT = Path("/dev/null/modok-unconfigured-project")

_REQUIRED_MATCH_FIELDS = (
    "project_slug",
    "source_system",
    "ticket_id",
)


# ---------------------------------------------------------------------------
# Pipeline entry point — run_ingest_event itself lives in modok.webhook.pipeline
# (imported above) to avoid a circular import with modok.ingestion.github; the
# per-kind helpers below stay here since existing tests patch their leaf
# dependencies (link_customer_issue_error_anchors, post_issue_comment, etc.)
# via modok.webhook.server.
# @spec WH-IDEM-003, WH-EXT-003
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# customer_issue — mechanical anchor linking + LLM fallback classification
# @spec SQ-ANCH-006, SQ-ANCH-007, SQ-LLMANCH-001, SQ-LLMANCH-002
# ---------------------------------------------------------------------------


async def _link_anchors_resilient(quine_client: Any, project_slug: str, node: CustomerIssue) -> None:
    """Resolve repo_root, run both mechanical anchor linkers, then the LLM
    fallback classifier if both found nothing.

    Each mechanical linker already degrades gracefully when registries can't
    be loaded (SQ-ANCH-005) — a resolution failure here (project not
    configured, config file absent, etc.) falls through to an
    intentionally-invalid repo_root rather than skipping the calls outright,
    so the calls always happen (SQ-ANCH-006) and each linker's own
    RegistryNotFoundError handling is the actual safety net.
    classify_customer_issue_anchors only runs when both linkers returned no
    matches (SQ-LLMANCH-001, SQ-LLMANCH-002).
    """
    try:
        from modok.cli.config import ModokConfig

        repo_root = Path(ModokConfig.load().project(project_slug).repo)
    except Exception as exc:
        print(
            f"anchor linking: could not resolve repo_root for {project_slug}: {exc}",
            file=sys.stderr,
        )
        repo_root = _UNCONFIGURED_REPO_ROOT

    matched_errors = await link_customer_issue_error_anchors(
        quine_client,
        project_slug,
        repo_root,
        node.source_system,
        node.ticket_id,
        node.raw_text,
    )
    matched_features = await link_customer_issue_feature_anchors(
        quine_client,
        project_slug,
        repo_root,
        node.source_system,
        node.ticket_id,
        node.raw_text,
    )
    # @spec SQ-LLMANCH-001, SQ-LLMANCH-002
    if not matched_errors and not matched_features:
        await classify_customer_issue_anchors(
            quine_client,
            project_slug,
            repo_root,
            node.source_system,
            node.ticket_id,
            node.raw_text,
        )


# ---------------------------------------------------------------------------
# investigation — standing-query match write-back
# @spec SQ-INV-001..006, SQ-GH-001..004
# ---------------------------------------------------------------------------


def _investigation_id(data: InvestigationData) -> str:
    return (
        f"{data.source_system}-{data.ticket_id}--{data.known_issue_id}--"
        f"{data.fix_id}--{data.standing_query_name}"
    )


async def _process_investigation(event: IngestEvent, quine_client: Any) -> int:
    data = event.data
    assert isinstance(data, InvestigationData)
    investigation_id = _investigation_id(data)

    # @spec SQ-INV-003 — already recorded: full no-op, no DRE call, no write-back
    if await quine_client.node_exists_by_parts(
        ("investigation", event.project_slug, investigation_id)
    ):
        return 0

    # @spec SQ-INV-004
    node = Investigation(
        node_type="Investigation",
        project_slug=event.project_slug,
        investigation_id=investigation_id,
        status="open",
        trigger_type="standing_query",
        triggered_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        standing_query_name=data.standing_query_name,
    )
    await quine_client.upsert_node(node)
    await quine_client.write_edge_by_parts(
        ("investigation", event.project_slug, investigation_id),
        "INVESTIGATES",
        ("customer-issue", event.project_slug, data.source_system, data.ticket_id),
    )

    await _maybe_notify_github(
        client=quine_client,
        project_slug=event.project_slug,
        source_system=data.source_system,
        ticket_id=data.ticket_id,
        investigation_id=investigation_id,
        standing_query_name=data.standing_query_name,
    )
    return 1


# ---------------------------------------------------------------------------
# milestone — accumulating Investigation/InvestigationMilestone evidence model
# @spec SQ-MILE-001 through SQ-MILE-012
# ---------------------------------------------------------------------------


def _milestone_investigation_id(data: MilestoneData) -> str:
    """Stable regardless of evidence (SQ-MILE-001) — a deliberately different,
    evidence-free identity scheme from _investigation_id above; the two
    coexist (see docs/llds/standing-queries.md § multiple-Investigation
    compatibility invariant)."""
    return f"{data.source_system}-{data.ticket_id}"


async def _investigation_has_milestone_kind(
    client: Any, project_slug: str, investigation_id: str, milestone_kind: str
) -> bool:
    rows = await client.query(
        "MATCH (i)-[:HAS_MILESTONE]->(m) WHERE id(i) = idFrom('investigation', $p, $inv) "
        "AND m.milestone_kind = $kind RETURN m LIMIT 1",
        {"p": project_slug, "inv": investigation_id, "kind": milestone_kind},
    )
    return bool(rows and rows[0])


async def _maybe_post_ci_corroboration_comment(
    client: Any, project_slug: str, data: MilestoneData
) -> None:
    """Best-effort: post a standalone GitHub comment noting additional
    evidence for the same issue. Never raises — same degrade-gracefully
    discipline as _maybe_notify_github (SQ-GH-001/004), applied to milestones
    per SQ-MILE-009/010/011."""
    if data.source_system != "github":
        return
    try:
        from modok.cli.config import ModokConfig

        project = ModokConfig.load().project(project_slug)
        github_repo = getattr(project, "github_repo", None)

        import os

        token = os.environ.get("GITHUB_TOKEN")
        if not github_repo or not token:
            return

        from modok.retrieval.formatting import format_ci_corroboration_milestone_markdown

        body = format_ci_corroboration_milestone_markdown(
            error_signature=data.error_signature,
            test_failure_id=data.test_failure_id,
            workflow_name=data.workflow_name,
            head_sha=data.head_sha,
            workflow_run_id=data.workflow_run_id,
        )
        await post_issue_comment(github_repo, token, data.ticket_id, body)
    except Exception as exc:
        print(
            f"CI-corroboration write-back failed for {project_slug}#{data.ticket_id}: {exc}",
            file=sys.stderr,
        )


async def _process_milestone(event: IngestEvent, quine_client: Any) -> int:
    data = event.data
    assert isinstance(data, MilestoneData)
    investigation_id = _milestone_investigation_id(data)

    # @spec SQ-MILE-001, SQ-MILE-002 — stable identity, unconditional get-or-create
    inv_node = Investigation(
        node_type="Investigation",
        project_slug=event.project_slug,
        investigation_id=investigation_id,
        status="open",
        trigger_type="standing_query",
        triggered_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        standing_query_name=data.standing_query_name,
    )
    await quine_client.upsert_node(inv_node)
    await quine_client.write_edge_by_parts(
        ("investigation", event.project_slug, investigation_id),
        "INVESTIGATES",
        ("customer-issue", event.project_slug, data.source_system, data.ticket_id),
    )

    # @spec SQ-MILE-003, SQ-MILE-004 — distinct milestone identity, dedup by it
    milestone_parts = (
        "investigation-milestone",
        event.project_slug,
        investigation_id,
        data.milestone_kind,
        data.test_failure_id,
        data.error_signature,
    )
    if await quine_client.node_exists_by_parts(milestone_parts):
        return 0

    # @spec SQ-MILE-009 — first-transition check runs before this milestone is recorded
    is_first_transition = not await _investigation_has_milestone_kind(
        quine_client, event.project_slug, investigation_id, data.milestone_kind
    )

    # @spec SQ-MILE-005 — milestone/evidence write always happens for a new milestone
    milestone_node = InvestigationMilestone(
        node_type="InvestigationMilestone",
        project_slug=event.project_slug,
        investigation_id=investigation_id,
        milestone_kind=data.milestone_kind,
        standing_query_name=data.standing_query_name,
        test_failure_id=data.test_failure_id,
        error_signature=data.error_signature,
        workflow_run_id=data.workflow_run_id,
        created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    await quine_client.upsert_node(milestone_node)
    await quine_client.write_edge_by_parts(
        ("investigation", event.project_slug, investigation_id),
        "HAS_MILESTONE",
        milestone_parts,
    )
    await quine_client.write_edge_by_parts(
        milestone_parts,
        "EVIDENCED_BY",
        ("test-failure", event.project_slug, data.workflow_run_id, data.test_failure_id),
    )

    # @spec SQ-MILE-009 — comment posted only on the first CI-corroboration transition
    if is_first_transition:
        await _maybe_post_ci_corroboration_comment(quine_client, event.project_slug, data)

    return 1


async def _maybe_notify_github(
    *,
    client: Any,
    project_slug: str,
    source_system: str,
    ticket_id: str,
    investigation_id: str,
    standing_query_name: str,
) -> None:
    """Best-effort: post the DRE's debug packet as a GitHub issue comment.

    @spec SQ-GH-001, SQ-GH-003, SQ-GH-004, SQ-GH-007
    """
    if source_system != "github":
        return

    try:
        from modok.cli.config import ModokConfig

        config = ModokConfig.load()
        project = next((p for p in config.projects if p.slug == project_slug), None)
        github_repo = getattr(project, "github_repo", None) if project else None

        import os

        token = os.environ.get("GITHUB_TOKEN")
        if not github_repo or not token:
            return

        from modok.retrieval.engine import quick_investigation_summary, retrieve

        # Resolve the real Quine node ID by property lookup — the CustomerIssue
        # was addressed via Quine's own idFrom() at write time (embedded in the
        # upsert_node Cypher), so there is no Python-computable ID for it; the
        # DRE's retrieve() needs the actual UUID Quine assigned, not a synthetic
        # one (see docs/llds/quine-client.md § node_exists_by_parts).
        rows = await client.query(
            "MATCH (n) WHERE n.node_type = 'CustomerIssue' AND n.project_slug = $p "
            "AND n.source_system = $s AND n.ticket_id = $t RETURN id(n) LIMIT 1",
            {"p": project_slug, "s": source_system, "t": ticket_id},
        )
        if not rows or not rows[0]:
            print(
                f"GitHub write-back: CustomerIssue not found for "
                f"{project_slug}/{source_system}#{ticket_id}",
                file=sys.stderr,
            )
            return
        ci_id = rows[0][0]

        # Load registry context so the DRE's LLM fallback (used whenever
        # graph-first anchors are absent) has feature/module slugs and
        # descriptions to work with — the same context modok retrieve's CLI
        # command loads. Without this, a ticket with no graph anchors yet
        # gets an almost-empty packet even when the LLM fallback runs.
        try:
            from modok.ingestion.registry import Registry

            registry = Registry(Path(project.repo)) if project else None
            feature_slugs = registry.feature_slugs() if registry else None
            module_slugs = registry.module_slugs() if registry else None
            valid_slugs = (
                feature_slugs + module_slugs if registry else None
            )
            feature_descriptions = registry.feature_descriptions() if registry else None
            module_descriptions = registry.module_descriptions() if registry else None
            module_elements = registry.module_elements() if registry else None
            module_source_files = registry.all_module_source_files() if registry else None
            feature_source_files = registry.all_feature_source_files() if registry else None
        except Exception:
            feature_slugs = module_slugs = valid_slugs = None
            feature_descriptions = module_descriptions = None
            module_elements = module_source_files = feature_source_files = None

        from modok.ingestion.github import post_issue_comment
        from modok.retrieval.formatting import (
            format_debug_packet_markdown,
            format_investigation_triggered_markdown,
        )

        # Post the fast "triggered" comment first, before the slow
        # traversal/scoring/LLM-summary work below — found live: a full
        # retrieve() can take several minutes with no visible feedback in
        # the meantime, and an earlier LLM-based version of this comment's
        # own summary measured ~85s on its own, largely defeating the head
        # start (the following retrieve() call's own summary landed on an
        # already-warm model and finished in seconds, so the two comments
        # arrived almost together). quick_investigation_summary is now a
        # pure graph/registry lookup with no LLM call, so this posts in
        # about the time of a couple of Quine round-trips. This comment's
        # own generation failing must not prevent it from posting at all
        # (falls back to no summary line) or block the results comment
        # that follows.
        try:
            quick_summary = await quick_investigation_summary(
                ci_id, project_slug, client, feature_source_files=feature_source_files
            )
        except Exception:
            quick_summary = ""
        triggered_body = format_investigation_triggered_markdown(
            quick_summary, standing_query_name, investigation_id
        )
        try:
            await post_issue_comment(github_repo, token, ticket_id, triggered_body)
        except Exception as exc:
            print(
                f"GitHub write-back (triggered comment) failed for "
                f"{project_slug}#{ticket_id}: {exc}",
                file=sys.stderr,
            )

        packet = await retrieve(
            ci_id,
            project_slug,
            client,
            valid_slugs=valid_slugs,
            feature_slugs=feature_slugs,
            module_slugs=module_slugs,
            feature_descriptions=feature_descriptions,
            module_descriptions=module_descriptions,
            module_elements=module_elements,
            module_source_files=module_source_files,
            feature_source_files=feature_source_files,
        )

        results_body = format_debug_packet_markdown(packet, investigation_id, standing_query_name)

        await post_issue_comment(github_repo, token, ticket_id, results_body)
    except Exception as exc:
        print(f"GitHub write-back failed for {project_slug}#{ticket_id}: {exc}", file=sys.stderr)


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
    # POST /standing-query/result
    # @spec SQ-ROUTE-001, SQ-ROUTE-002, SQ-ROUTE-003, SQ-ROUTE-004, SQ-ROUTE-005
    # Not a push adapter — Quine's PostToEndpoint output posts to one static
    # URL (no per-project templating), so project_slug travels in the body.
    # No authentication: reachable only from the co-located Quine instance.
    # ---------------------------------------------------------------------------

    @app.post("/standing-query/result")
    async def standing_query_result(request: Request) -> Response:
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"detail": "Invalid JSON"}, status_code=400)

        matches = payload if isinstance(payload, list) else [payload]

        slugs = app.state.known_project_slugs
        investigations_written = 0
        for match in matches:
            if not isinstance(match, dict):
                return JSONResponse({"detail": "Invalid match object"}, status_code=400)

            # @spec SQ-ROUTE-006 — Quine's default output structure wraps the
            # enrichment query's row in {"meta": {...}, "data": {...}}; unwrap
            # it if present. Confirmed live against Quine 1.10.0's default
            # PostToEndpoint structure (docs/llds/standing-queries.md § Live
            # Verification Findings).
            if "data" in match and "meta" in match and isinstance(match["data"], dict):
                match = match["data"]

            missing = [f for f in _REQUIRED_MATCH_FIELDS if not match.get(f)]
            if missing:
                return JSONResponse(
                    {"detail": f"Missing required fields: {missing}"}, status_code=400
                )

            project_slug = match["project_slug"]
            if slugs is not None and project_slug not in slugs:
                return JSONResponse({"detail": f"Unknown project: {project_slug}"}, status_code=404)

            # @spec SQ-ROUTE-007 — dispatch on payload shape, not a second route:
            # the ci-corroboration-pattern's enrichment includes milestone_kind,
            # existing patterns' enrichments don't.
            milestone_kind = match.get("milestone_kind")
            if milestone_kind:
                event = IngestEvent(
                    kind="milestone",
                    project_slug=project_slug,
                    data=MilestoneData(
                        source_system=match["source_system"],
                        ticket_id=match["ticket_id"],
                        milestone_kind=milestone_kind,
                        standing_query_name=match.get(
                            "standing_query_name", "ci-corroboration-pattern"
                        ),
                        workflow_run_id=match.get("workflow_run_id", ""),
                        test_failure_id=match.get("test_failure_id", ""),
                        error_signature=match.get("error_signature", ""),
                        workflow_name=match.get("workflow_name", ""),
                        head_sha=match.get("head_sha", ""),
                    ),
                )
            else:
                event = IngestEvent(
                    kind="investigation",
                    project_slug=project_slug,
                    data=InvestigationData(
                        source_system=match["source_system"],
                        ticket_id=match["ticket_id"],
                        known_issue_id=match.get("known_issue_id", ""),
                        fix_id=match.get("fix_id", ""),
                        standing_query_name=match.get(
                            "standing_query_name", "actionable-issue-pattern"
                        ),
                    ),
                )
            investigations_written += await asyncio.to_thread(run_ingest_event, event, quine_client)

        return JSONResponse({"status": "ok", "investigations_written": investigations_written})

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
    # @spec WH-SERVE-002, WH-SERVE-003, WH-SERVE-007
    from modok.cli.config import ModokConfig
    return ModokConfig.load().webhook


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
