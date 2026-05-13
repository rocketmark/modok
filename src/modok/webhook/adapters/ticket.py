from __future__ import annotations

from pydantic import BaseModel, ValidationError

from modok.webhook.errors import WebhookAuthError
from modok.webhook.models import CustomerIssueData, IngestEvent, WebhookConfig


class _TicketPayload(BaseModel):
    ticket_id: str
    summary: str
    body: str = ""
    source_system: str = "webhook"

    model_config = {"extra": "ignore"}


class GenericTicketAdapter:
    # @spec WH-TKT-001, WH-TKT-002, WH-TKT-003, WH-TKT-004

    def verify_request(self, request: object, config: WebhookConfig) -> None:
        # @spec WH-TKT-001
        # Headers are lowercased by HTTP/1.1 convention; check both forms.
        headers: dict = getattr(request, "headers", {})
        auth: str = headers.get("authorization") or headers.get("Authorization") or ""
        expected = f"Bearer {config.bearer_token}"
        if not auth or auth != expected:
            raise WebhookAuthError("Missing or invalid bearer token")

    def normalize_event(self, payload: dict, event_type: str) -> IngestEvent | None:
        # @spec WH-TKT-002
        project_slug = getattr(payload, "__project_slug__", "")
        parsed = _TicketPayload.model_validate(payload)
        return IngestEvent(
            kind="customer_issue",
            project_slug=project_slug,
            data=CustomerIssueData(
                ticket_id=parsed.ticket_id,
                summary=parsed.summary,
                raw_text=parsed.body,
                status="open",
                source_system=parsed.source_system,
            ),
        )
