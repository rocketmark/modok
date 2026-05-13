"""
Tests for modok.webhook — the HTTP webhook receiver.
All tests written before implementation (Phase 5). Every test cites
the EARS spec it verifies via @spec annotation.

Specs verified:
  WH-SERVE-001, WH-SERVE-002, WH-SERVE-003, WH-SERVE-004,
  WH-SERVE-005, WH-SERVE-006, WH-SERVE-007,
  WH-ROUTE-001, WH-ROUTE-002, WH-ROUTE-003, WH-ROUTE-004,
  WH-PUSH-001, WH-PUSH-003, WH-PUSH-004,
  WH-PUSH-005, WH-PUSH-006, WH-PUSH-007, WH-PUSH-008, WH-PUSH-009,
  WH-PULL-001, WH-PULL-002, WH-PULL-003, WH-PULL-004, WH-PULL-005,
  WH-GH-001, WH-GH-002, WH-GH-003, WH-GH-004, WH-GH-005,
  WH-GH-006, WH-GH-007, WH-GH-008, WH-GH-009,
  WH-TKT-001, WH-TKT-002, WH-TKT-003, WH-TKT-004,
  WH-IDEM-001, WH-IDEM-002, WH-IDEM-003,
  WH-EXT-001, WH-EXT-002, WH-EXT-003.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from modok.webhook.adapters.base import PullAdapter, PushAdapter
from modok.webhook.errors import WebhookAuthError
from modok.webhook.models import (
    CustomerIssueData,
    FixData,
    IngestEvent,
    WebhookConfig,
)
from modok.webhook.router import PULL_ADAPTERS, PUSH_ADAPTERS
from modok.webhook.server import build_app, run_ingest_event, start_pull_adapters, stop_pull_adapters


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    github_secret: str = "test-secret",
    bearer_token: str = "test-token",
    enabled_sources: list[str] | None = None,
    port: int = 4242,
) -> WebhookConfig:
    return WebhookConfig(
        github_secret=github_secret,
        bearer_token=bearer_token,
        enabled_sources=enabled_sources,
        port=port,
    )


def _gh_signature(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _make_issue_payload(
    number: int = 1,
    title: str = "Test issue",
    body: str = "Some description",
    state: str = "open",
    action: str = "opened",
) -> dict:
    return {
        "action": action,
        "issue": {
            "number": number,
            "title": title,
            "body": body,
            "state": state,
        },
    }


def _make_pr_payload(
    number: int = 42,
    title: str = "Fix the thing",
    merged: bool = True,
    action: str = "closed",
) -> dict:
    return {
        "action": action,
        "merged": merged,
        "pull_request": {
            "number": number,
            "title": title,
        },
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def config():
    return _make_config()


@pytest.fixture()
def mock_quine():
    client = AsyncMock()
    client.ping = AsyncMock(return_value=True)
    client.upsert_node = AsyncMock(return_value=None)
    return client


@pytest.fixture()
def app(config, mock_quine):
    return build_app(
        config=config,
        quine_client=mock_quine,
        known_project_slugs={"test-project"},
    )


@pytest.fixture()
def client(app):
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# modok serve — startup validation
# ---------------------------------------------------------------------------


def test_serve_config_missing_exits_1():
    # @spec WH-SERVE-001
    with patch("modok.webhook.server.load_config", side_effect=FileNotFoundError):
        with pytest.raises(SystemExit) as exc_info:
            from modok.webhook.server import serve_main
            serve_main([])
    assert exc_info.value.code == 1


def test_serve_github_secret_missing_exits_1():
    # @spec WH-SERVE-002
    cfg = _make_config(github_secret="", enabled_sources=["github"])
    with pytest.raises(SystemExit) as exc_info:
        from modok.webhook.server import validate_config
        validate_config(cfg)
    assert exc_info.value.code == 1


def test_serve_bearer_token_missing_exits_1():
    # @spec WH-SERVE-003
    cfg = _make_config(bearer_token="", enabled_sources=["ticket"])
    with pytest.raises(SystemExit) as exc_info:
        from modok.webhook.server import validate_config
        validate_config(cfg)
    assert exc_info.value.code == 1


def test_serve_enabled_sources_skips_unconfigured_secret():
    # @spec WH-SERVE-007
    # github not in enabled_sources — missing github_secret should not fail
    cfg = _make_config(github_secret="", enabled_sources=["ticket"])
    from modok.webhook.server import validate_config
    validate_config(cfg)  # must not raise


def test_serve_enabled_sources_absent_enables_all():
    # @spec WH-SERVE-007
    # enabled_sources absent — all adapters active, all secrets required
    cfg = _make_config(github_secret="", enabled_sources=None)
    with pytest.raises(SystemExit):
        from modok.webhook.server import validate_config
        validate_config(cfg)


def test_serve_quine_unreachable_exits_2():
    # @spec WH-SERVE-004
    cfg = _make_config()
    mock_q = AsyncMock()
    mock_q.ping = AsyncMock(return_value=False)
    with pytest.raises(SystemExit) as exc_info:
        from modok.webhook.server import check_quine
        asyncio.run(check_quine(mock_q, cfg))
    assert exc_info.value.code == 2


def test_serve_startup_logs_address_to_stderr(capsys, config, mock_quine):
    # @spec WH-SERVE-005
    build_app(config=config, quine_client=mock_quine)
    captured = capsys.readouterr()
    assert "127.0.0.1" in captured.err
    assert "4242" in captured.err


def test_serve_port_override(config, mock_quine):
    # @spec WH-SERVE-006
    app = build_app(config=config, quine_client=mock_quine, host="0.0.0.0", port=9999)
    assert app.state.host == "0.0.0.0"
    assert app.state.port == 9999


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_unknown_project_slug_returns_404(client):
    # @spec WH-ROUTE-001
    resp = client.post("/webhook/no-such-project/github", json={})
    assert resp.status_code == 404


def test_unknown_source_returns_404(client):
    # @spec WH-ROUTE-002
    resp = client.post("/webhook/test-project/no-such-source", json={})
    assert resp.status_code == 404


def test_disabled_source_returns_404():
    # @spec WH-ROUTE-002, WH-SERVE-007
    cfg = _make_config(enabled_sources=["github"])  # ticket not enabled
    mock_q = AsyncMock()
    mock_q.ping = AsyncMock(return_value=True)
    app = build_app(config=cfg, quine_client=mock_q, known_project_slugs={"test-project"})
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/webhook/test-project/ticket", json={})
    assert resp.status_code == 404


def test_health_returns_200_with_quine_status(client):
    # @spec WH-ROUTE-003
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
    assert "quine" in data
    assert data["status"] == "ok"
    assert isinstance(data["quine"], bool)


def test_health_quine_field_is_cached_not_live(config, mock_quine):
    # @spec WH-ROUTE-003
    app = build_app(config=config, quine_client=mock_quine, known_project_slugs={"test-project"})
    # Set cached status to False explicitly
    app.state.quine_healthy = False
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.get("/health")
    assert resp.json()["quine"] is False
    # ping was NOT called for /health
    mock_quine.ping.assert_not_called()


def test_successful_pipeline_updates_cached_quine_status(client, app):
    # @spec WH-ROUTE-004
    app.state.quine_healthy = False
    body = json.dumps(_make_issue_payload()).encode()
    sig = _gh_signature(body, "test-secret")
    with patch("modok.webhook.server.run_ingest_event", return_value=1):
        resp = client.post(
            "/webhook/test-project/github",
            content=body,
            headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": sig,
                     "Content-Type": "application/json"},
        )
    assert resp.status_code == 200
    assert app.state.quine_healthy is True


# ---------------------------------------------------------------------------
# Push adapter protocol
# ---------------------------------------------------------------------------


def test_push_adapter_verify_request_raises_on_bad_auth():
    # @spec WH-PUSH-001
    from modok.webhook.adapters.github import GitHubAdapter
    adapter = GitHubAdapter()
    request = MagicMock()
    request.headers = {"X-Hub-Signature-256": "sha256=bad"}
    request.body_bytes = b'{"action": "opened"}'
    cfg = _make_config()
    with pytest.raises(WebhookAuthError):
        adapter.verify_request(request, cfg)



def test_skip_event_returns_200_skipped(client):
    # @spec WH-PUSH-003
    body = json.dumps(_make_pr_payload(merged=False, action="opened")).encode()
    sig = _gh_signature(body, "test-secret")
    resp = client.post(
        "/webhook/test-project/github",
        content=body,
        headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sig,
                 "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"


def test_bad_auth_returns_401(client):
    # @spec WH-PUSH-004
    body = json.dumps(_make_issue_payload()).encode()
    resp = client.post(
        "/webhook/test-project/github",
        content=body,
        headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": "sha256=bad",
                 "Content-Type": "application/json"},
    )
    assert resp.status_code == 401


def test_malformed_json_returns_400(client):
    # @spec WH-PUSH-005
    body = b"not valid json {"
    sig = _gh_signature(body, "test-secret")
    resp = client.post(
        "/webhook/test-project/github",
        content=body,
        headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": sig,
                 "Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_pipeline_exception_returns_500(client):
    # @spec WH-PUSH-006
    body = json.dumps(_make_issue_payload()).encode()
    sig = _gh_signature(body, "test-secret")
    with patch("modok.webhook.server.run_ingest_event", side_effect=RuntimeError("quine down")):
        resp = client.post(
            "/webhook/test-project/github",
            content=body,
            headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": sig,
                     "Content-Type": "application/json"},
        )
    assert resp.status_code == 500
    assert resp.json()["status"] == "error"
    assert "detail" in resp.json()


def test_successful_pipeline_returns_200_ok(client):
    # @spec WH-PUSH-007
    body = json.dumps(_make_issue_payload()).encode()
    sig = _gh_signature(body, "test-secret")
    with patch("modok.webhook.server.run_ingest_event", return_value=1):
        resp = client.post(
            "/webhook/test-project/github",
            content=body,
            headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": sig,
                     "Content-Type": "application/json"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["nodes_written"] == 1


def test_pipeline_called_via_to_thread():
    # @spec WH-PUSH-008
    # Verify asyncio.to_thread is used by patching it at the asyncio module level
    # as imported inside the server's route handler.
    import modok.webhook.server as wh_server
    original = asyncio.to_thread
    call_log: list = []

    async def _mock_to_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
        call_log.append(fn)
        return fn(*args, **kwargs)

    wh_server.asyncio.to_thread = _mock_to_thread  # type: ignore[attr-defined]
    try:
        cfg = _make_config()
        mock_q = AsyncMock()
        mock_q.upsert_node = AsyncMock(return_value=None)
        app = build_app(config=cfg, quine_client=mock_q, known_project_slugs={"test-project"})
        c = TestClient(app, raise_server_exceptions=False)
        body = json.dumps(_make_issue_payload()).encode()
        sig = _gh_signature(body, "test-secret")
        c.post(
            "/webhook/test-project/github",
            content=body,
            headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": sig,
                     "Content-Type": "application/json"},
        )
        assert len(call_log) == 1
        assert call_log[0] is run_ingest_event
    finally:
        wh_server.asyncio.to_thread = original  # type: ignore[attr-defined]


def test_raw_body_read_before_json_parse():
    # @spec WH-PUSH-009
    # Confirm verify_request is called with raw bytes before JSON parsing occurs.
    # We verify this by checking that a valid HMAC on the raw bytes passes even
    # when the JSON contains unicode that would be re-encoded differently.
    raw_body = '{"action": "opened", "issue": {"number": 1, "title": "héllo", "body": "", "state": "open"}}'.encode("utf-8")
    sig = _gh_signature(raw_body, "test-secret")
    cfg = _make_config()
    mock_q = AsyncMock()
    mock_q.ping = AsyncMock(return_value=True)
    app = build_app(config=cfg, quine_client=mock_q)
    c = TestClient(app, raise_server_exceptions=False)
    with patch("modok.webhook.server.run_ingest_event", return_value=1):
        resp = c.post(
            "/webhook/test-project/github",
            content=raw_body,
            headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": sig,
                     "Content-Type": "application/json"},
        )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Pull adapter protocol
# ---------------------------------------------------------------------------


class _StubPullAdapter:
    """Minimal PullAdapter for testing lifecycle calls."""

    def __init__(self):
        self.started = False
        self.stopped = False
        self._on_event: Callable | None = None

    async def start(self, config: WebhookConfig, on_event: Callable[[IngestEvent], Awaitable[None]]) -> None:
        # @spec WH-PULL-001
        self.started = True
        self._on_event = on_event

    async def stop(self) -> None:
        # @spec WH-PULL-002
        self.stopped = True


@pytest.mark.asyncio
async def test_pull_adapter_start_called_on_startup():
    # @spec WH-PULL-003
    adapter = _StubPullAdapter()
    cfg = _make_config()
    mock_q = AsyncMock()
    mock_q.ping = AsyncMock(return_value=True)
    await start_pull_adapters({"stub": adapter}, cfg, mock_q)
    assert adapter.started is True


@pytest.mark.asyncio
async def test_pull_adapter_stop_called_on_shutdown():
    # @spec WH-PULL-004
    adapter = _StubPullAdapter()
    cfg = _make_config()
    mock_q = AsyncMock()
    mock_q.ping = AsyncMock(return_value=True)
    await start_pull_adapters({"stub": adapter}, cfg, mock_q)
    await stop_pull_adapters({"stub": adapter})
    assert adapter.stopped is True


@pytest.mark.asyncio
async def test_pull_adapter_on_event_is_async_wrapper():
    # @spec WH-PULL-005
    # on_event must be awaitable — pull adapters should not need asyncio.to_thread
    adapter = _StubPullAdapter()
    cfg = _make_config()
    mock_q = AsyncMock()
    mock_q.ping = AsyncMock(return_value=True)
    await start_pull_adapters({"stub": adapter}, cfg, mock_q)
    assert adapter._on_event is not None
    # Confirm it is a coroutine function (async wrapper)
    import inspect
    assert inspect.iscoroutinefunction(adapter._on_event)


@pytest.mark.asyncio
async def test_pull_adapter_on_event_calls_same_pipeline_as_push():
    # @spec WH-PULL-005
    adapter = _StubPullAdapter()
    cfg = _make_config()
    mock_q = AsyncMock()
    mock_q.ping = AsyncMock(return_value=True)
    with patch("modok.webhook.server.run_ingest_event", return_value=1) as mock_pipeline:
        await start_pull_adapters({"stub": adapter}, cfg, mock_q)
        event = IngestEvent(
            kind="customer_issue",
            project_slug="test-project",
            data=CustomerIssueData(
                ticket_id="TKT-1", summary="Test", raw_text="", status="open",
                source_system="webhook",
            ),
        )
        await adapter._on_event(event)
        mock_pipeline.assert_called_once()


# ---------------------------------------------------------------------------
# GitHub adapter
# ---------------------------------------------------------------------------


def test_gh_hmac_verification_rejects_bad_signature():
    # @spec WH-GH-001
    from modok.webhook.adapters.github import GitHubAdapter
    adapter = GitHubAdapter()
    request = MagicMock()
    request.headers = {"X-Hub-Signature-256": "sha256=aaaa"}
    request.body_bytes = b'{"action": "opened"}'
    with pytest.raises(WebhookAuthError):
        adapter.verify_request(request, _make_config())


def test_gh_hmac_verification_rejects_missing_header():
    # @spec WH-GH-001
    from modok.webhook.adapters.github import GitHubAdapter
    adapter = GitHubAdapter()
    request = MagicMock()
    request.headers = {}
    request.body_bytes = b'{"action": "opened"}'
    with pytest.raises(WebhookAuthError):
        adapter.verify_request(request, _make_config())


def test_gh_hmac_verification_accepts_valid_signature():
    # @spec WH-GH-001
    from modok.webhook.adapters.github import GitHubAdapter
    adapter = GitHubAdapter()
    body = b'{"action": "opened"}'
    sig = _gh_signature(body, "test-secret")
    request = MagicMock()
    request.headers = {"X-Hub-Signature-256": sig}
    request.body_bytes = body
    adapter.verify_request(request, _make_config())  # must not raise


def test_gh_ping_event_returns_skip(client):
    # @spec WH-GH-002
    body = json.dumps({"hook_id": 123, "zen": "Keep it logically awesome."}).encode()
    sig = _gh_signature(body, "test-secret")
    resp = client.post(
        "/webhook/test-project/github",
        content=body,
        headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": sig,
                 "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"


def test_gh_ping_verifies_hmac(client):
    # @spec WH-GH-002 — ping must still verify HMAC (bad sig → 401)
    body = json.dumps({"hook_id": 123}).encode()
    resp = client.post(
        "/webhook/test-project/github",
        content=body,
        headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": "sha256=bad",
                 "Content-Type": "application/json"},
    )
    assert resp.status_code == 401


@pytest.mark.parametrize("action", ["opened", "edited", "labeled", "closed", "reopened"])
def test_gh_issue_actions_produce_customer_issue(action):
    # @spec WH-GH-003
    from modok.webhook.adapters.github import GitHubAdapter
    adapter = GitHubAdapter()
    payload = _make_issue_payload(action=action)
    event = adapter.normalize_event(payload, "issues")
    assert event is not None
    assert event.kind == "customer_issue"


def test_gh_merged_pr_produces_fix():
    # @spec WH-GH-004
    from modok.webhook.adapters.github import GitHubAdapter
    adapter = GitHubAdapter()
    payload = _make_pr_payload(merged=True, action="closed")
    event = adapter.normalize_event(payload, "pull_request")
    assert event is not None
    assert event.kind == "fix"


@pytest.mark.parametrize("action", ["opened", "reopened", "edited", "synchronize"])
def test_gh_unmerged_pr_actions_produce_skip(action):
    # @spec WH-GH-005
    from modok.webhook.adapters.github import GitHubAdapter
    adapter = GitHubAdapter()
    payload = _make_pr_payload(merged=False, action=action)
    event = adapter.normalize_event(payload, "pull_request")
    assert event is not None
    assert event.kind == "skip"


def test_gh_closed_unmerged_pr_produces_skip():
    # @spec WH-GH-005
    from modok.webhook.adapters.github import GitHubAdapter
    adapter = GitHubAdapter()
    payload = _make_pr_payload(merged=False, action="closed")
    event = adapter.normalize_event(payload, "pull_request")
    assert event.kind == "skip"


def test_gh_unknown_event_type_produces_skip():
    # @spec WH-GH-006
    from modok.webhook.adapters.github import GitHubAdapter
    adapter = GitHubAdapter()
    event = adapter.normalize_event({"action": "created"}, "deployment")
    assert event is not None
    assert event.kind == "skip"


def test_gh_issue_field_mapping():
    # @spec WH-GH-007
    from modok.webhook.adapters.github import GitHubAdapter
    adapter = GitHubAdapter()
    payload = _make_issue_payload(number=42, title="Dropout bug", body="Details here", state="open")
    event = adapter.normalize_event(payload, "issues")
    assert event is not None
    assert isinstance(event.data, CustomerIssueData)
    assert event.data.ticket_id == "42"
    assert event.data.summary == "Dropout bug"
    assert event.data.raw_text == "Details here"
    assert event.data.status == "open"


def test_gh_issue_null_body_becomes_empty_string():
    # @spec WH-GH-007
    from modok.webhook.adapters.github import GitHubAdapter
    adapter = GitHubAdapter()
    payload = _make_issue_payload()
    payload["issue"]["body"] = None
    event = adapter.normalize_event(payload, "issues")
    assert event is not None
    assert event.data.raw_text == ""


def test_gh_pr_field_mapping():
    # @spec WH-GH-008
    from modok.webhook.adapters.github import GitHubAdapter
    adapter = GitHubAdapter()
    payload = _make_pr_payload(number=99, title="Fix dropout", merged=True, action="closed")
    event = adapter.normalize_event(payload, "pull_request")
    assert event is not None
    assert isinstance(event.data, FixData)
    assert event.data.fix_id == "gh-99"
    assert event.data.summary == "Fix dropout"


@given(
    number=st.integers(min_value=1, max_value=10_000),
    title=st.text(min_size=1, max_size=200),
    body=st.one_of(st.none(), st.text(max_size=500)),
    state=st.sampled_from(["open", "closed"]),
    action=st.sampled_from(["opened", "edited", "labeled", "closed", "reopened"]),
)
@settings(max_examples=50)
def test_gh_normalization_is_deterministic_issues(number, title, body, state, action):
    # @spec WH-GH-009
    from modok.webhook.adapters.github import GitHubAdapter
    adapter = GitHubAdapter()
    payload = {
        "action": action,
        "issue": {"number": number, "title": title, "body": body, "state": state},
    }
    event1 = adapter.normalize_event(payload, "issues")
    event2 = adapter.normalize_event(payload, "issues")
    assert event1 == event2


@given(
    number=st.integers(min_value=1, max_value=10_000),
    title=st.text(min_size=1, max_size=200),
    merged=st.booleans(),
)
@settings(max_examples=50)
def test_gh_normalization_is_deterministic_prs(number, title, merged):
    # @spec WH-GH-009
    from modok.webhook.adapters.github import GitHubAdapter
    adapter = GitHubAdapter()
    payload = _make_pr_payload(number=number, title=title, merged=merged, action="closed")
    event1 = adapter.normalize_event(payload, "pull_request")
    event2 = adapter.normalize_event(payload, "pull_request")
    assert event1 == event2


# ---------------------------------------------------------------------------
# Generic ticket adapter
# ---------------------------------------------------------------------------


def test_ticket_bearer_token_verified():
    # @spec WH-TKT-001
    from modok.webhook.adapters.ticket import GenericTicketAdapter
    adapter = GenericTicketAdapter()
    request = MagicMock()
    request.headers = {"Authorization": "Bearer wrong-token"}
    with pytest.raises(WebhookAuthError):
        adapter.verify_request(request, _make_config())


def test_ticket_missing_auth_header_raises():
    # @spec WH-TKT-001
    from modok.webhook.adapters.ticket import GenericTicketAdapter
    adapter = GenericTicketAdapter()
    request = MagicMock()
    request.headers = {}
    with pytest.raises(WebhookAuthError):
        adapter.verify_request(request, _make_config())


def test_ticket_valid_bearer_token_passes():
    # @spec WH-TKT-001
    from modok.webhook.adapters.ticket import GenericTicketAdapter
    adapter = GenericTicketAdapter()
    request = MagicMock()
    request.headers = {"Authorization": "Bearer test-token"}
    adapter.verify_request(request, _make_config())  # must not raise


def test_ticket_full_payload_accepted():
    # @spec WH-TKT-002
    from modok.webhook.adapters.ticket import GenericTicketAdapter
    adapter = GenericTicketAdapter()
    payload = {"ticket_id": "TKT-1", "summary": "Dropout", "body": "Details", "source_system": "zendesk"}
    event = adapter.normalize_event(payload, "ticket")
    assert event is not None
    assert event.kind == "customer_issue"
    assert event.data.ticket_id == "TKT-1"
    assert event.data.source_system == "zendesk"


def test_ticket_optional_fields_default():
    # @spec WH-TKT-002
    from modok.webhook.adapters.ticket import GenericTicketAdapter
    adapter = GenericTicketAdapter()
    payload = {"ticket_id": "TKT-2", "summary": "Another issue"}
    event = adapter.normalize_event(payload, "ticket")
    assert event.data.raw_text == ""
    assert event.data.source_system == "webhook"


def test_ticket_unknown_fields_ignored():
    # @spec WH-TKT-002
    from modok.webhook.adapters.ticket import GenericTicketAdapter
    adapter = GenericTicketAdapter()
    payload = {"ticket_id": "TKT-3", "summary": "Issue", "unknown_field": "ignored"}
    event = adapter.normalize_event(payload, "ticket")
    assert event is not None
    assert event.data.ticket_id == "TKT-3"
    assert event.data.summary == "Issue"
    assert not hasattr(event.data, "unknown_field")


def test_ticket_missing_required_fields_returns_400(client):
    # @spec WH-TKT-003
    # Auth passes (valid bearer token); missing required fields → 400
    body = json.dumps({"body": "No ticket_id or summary"}).encode()
    resp = client.post(
        "/webhook/test-project/ticket",
        content=body,
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400


def test_ticket_missing_summary_returns_400(client):
    # @spec WH-TKT-003
    body = json.dumps({"ticket_id": "TKT-1"}).encode()
    resp = client.post(
        "/webhook/test-project/ticket",
        content=body,
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400


@given(
    ticket_id=st.text(min_size=1, max_size=50),
    summary=st.text(min_size=1, max_size=200),
    body=st.text(max_size=500),
    source_system=st.text(max_size=50),
)
@settings(max_examples=50)
def test_ticket_normalization_is_deterministic(ticket_id, summary, body, source_system):
    # @spec WH-TKT-004
    from modok.webhook.adapters.ticket import GenericTicketAdapter
    adapter = GenericTicketAdapter()
    payload = {"ticket_id": ticket_id, "summary": summary, "body": body, "source_system": source_system}
    event1 = adapter.normalize_event(payload, "ticket")
    event2 = adapter.normalize_event(payload, "ticket")
    assert event1 == event2


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@given(
    number=st.integers(min_value=1, max_value=10_000),
    title=st.text(min_size=1, max_size=200),
)
@settings(max_examples=30)
def test_duplicate_github_event_no_duplicate_nodes(number, title):
    # @spec WH-IDEM-001
    from modok.webhook.server import run_ingest_event
    from modok.webhook.adapters.github import GitHubAdapter
    adapter = GitHubAdapter()
    payload = _make_issue_payload(number=number, title=title)
    event = adapter.normalize_event(payload, "issues")
    mock_q = AsyncMock()
    mock_q.upsert_node = AsyncMock(return_value=None)
    run_ingest_event(event, mock_q)
    run_ingest_event(event, mock_q)
    calls = mock_q.upsert_node.call_args_list
    assert len(calls) == 2
    # Both calls must target a node with identical (project_slug, source_system, ticket_id)
    # — the upsert key — so the second call is a no-op update, not a new insert.
    node_first: CustomerIssue = calls[0].args[0]
    node_second: CustomerIssue = calls[1].args[0]
    assert node_first.project_slug == node_second.project_slug
    assert node_first.source_system == node_second.source_system
    assert node_first.ticket_id == node_second.ticket_id


def test_quine_unreachable_returns_500_for_retry(client):
    # @spec WH-IDEM-002
    body = json.dumps(_make_issue_payload()).encode()
    sig = _gh_signature(body, "test-secret")
    with patch("modok.webhook.server.run_ingest_event", side_effect=ConnectionError("quine down")):
        resp = client.post(
            "/webhook/test-project/github",
            content=body,
            headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": sig,
                     "Content-Type": "application/json"},
        )
    assert resp.status_code == 500


def test_ingest_event_uses_upsert_not_insert():
    # @spec WH-IDEM-003
    from modok.webhook.server import run_ingest_event
    event = IngestEvent(
        kind="customer_issue",
        project_slug="test-project",
        data=CustomerIssueData(
            ticket_id="TKT-1", summary="Issue", raw_text="", status="open",
            source_system="webhook",
        ),
    )
    mock_q = AsyncMock()
    mock_q.upsert_node = AsyncMock(return_value=None)
    mock_q.insert_node = MagicMock()  # must NOT be called
    run_ingest_event(event, mock_q)
    mock_q.upsert_node.assert_called()
    mock_q.insert_node.assert_not_called()


# ---------------------------------------------------------------------------
# Adapter extension invariants
# ---------------------------------------------------------------------------


def test_registered_push_adapter_is_routable():
    # @spec WH-EXT-001
    # A push adapter registered under key "test-source" must be reachable
    # at POST /webhook/test-project/test-source
    class _EchoAdapter:
        def verify_request(self, request, config):
            pass

        def normalize_event(self, payload, event_type):
            return IngestEvent(
                kind="skip", project_slug="test-project", data=None
            )

    cfg = _make_config()
    mock_q = AsyncMock()
    mock_q.ping = AsyncMock(return_value=True)
    app = build_app(
        config=cfg,
        quine_client=mock_q,
        extra_push_adapters={"test-source": _EchoAdapter()},
    )
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post(
        "/webhook/test-project/test-source",
        json={"action": "opened"},
    )
    # 200 skipped — adapter returned skip; routing worked without server.py changes
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"


@pytest.mark.asyncio
async def test_registered_pull_adapter_lifecycle():
    # @spec WH-EXT-002
    adapter = _StubPullAdapter()
    cfg = _make_config()
    mock_q = AsyncMock()
    mock_q.ping = AsyncMock(return_value=True)
    await start_pull_adapters({"test-pull": adapter}, cfg, mock_q)
    assert adapter.started is True
    await stop_pull_adapters({"test-pull": adapter})
    assert adapter.stopped is True


def test_ingest_event_no_adapter_branching_in_pipeline():
    # @spec WH-EXT-003
    # run_ingest_event must route customer_issue and fix events purely on
    # IngestEvent.kind — no branching on source_system or adapter identity.
    # Verified by constructing the same kind from two different source_systems
    # and confirming the node ID prefix matches kind, not source.
    from modok.webhook.server import run_ingest_event

    issue_from_github = IngestEvent(
        kind="customer_issue",
        project_slug="test-project",
        data=CustomerIssueData(
            ticket_id="1", summary="GH issue", raw_text="", status="open",
            source_system="github",
        ),
    )
    issue_from_ticket = IngestEvent(
        kind="customer_issue",
        project_slug="test-project",
        data=CustomerIssueData(
            ticket_id="1", summary="GH issue", raw_text="", status="open",
            source_system="webhook",
        ),
    )

    mock_gh = AsyncMock()
    mock_gh.upsert_node = AsyncMock(return_value=None)
    mock_tkt = AsyncMock()
    mock_tkt.upsert_node = AsyncMock(return_value=None)

    run_ingest_event(issue_from_github, mock_gh)
    run_ingest_event(issue_from_ticket, mock_tkt)

    # The node written must be a CustomerIssue node regardless of source_system
    gh_node = mock_gh.upsert_node.call_args.args[0]
    tkt_node = mock_tkt.upsert_node.call_args.args[0]
    assert type(gh_node).__name__ == "CustomerIssue"
    assert type(tkt_node).__name__ == "CustomerIssue"
    # source_system differs but node type must not — no adapter-identity branching
    assert gh_node.source_system != tkt_node.source_system
    assert type(gh_node) == type(tkt_node)
