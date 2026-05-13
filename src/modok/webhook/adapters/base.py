from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from modok.webhook.models import IngestEvent, WebhookConfig


class PushAdapter(Protocol):
    # @spec WH-PUSH-001, WH-PUSH-002
    def verify_request(self, request: object, config: WebhookConfig) -> None:
        """Raise WebhookAuthError if the request cannot be verified."""
        ...

    def normalize_event(self, payload: dict, event_type: str) -> IngestEvent | None:
        """Map source payload to IngestEvent. Return None to silently skip."""
        ...


class PullAdapter(Protocol):
    # @spec WH-PULL-001, WH-PULL-002
    async def start(
        self,
        config: WebhookConfig,
        on_event: Callable[[IngestEvent], Awaitable[None]],
    ) -> None:
        """Connect to source and begin consuming. Call on_event for each event."""
        ...

    async def stop(self) -> None:
        """Gracefully disconnect."""
        ...
