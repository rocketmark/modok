from __future__ import annotations

from modok.webhook.adapters.github import GitHubAdapter
from modok.webhook.adapters.ticket import GenericTicketAdapter

# @spec WH-EXT-001, WH-EXT-002
# To add a new push adapter: implement verify_request + normalize_event, add an entry here.
# To add a new pull adapter: implement async start + stop, add an entry here.
# No other files need to change.

PUSH_ADAPTERS: dict[str, object] = {
    "github": GitHubAdapter(),
    "ticket": GenericTicketAdapter(),
    # "jira":   JiraAdapter(),     ← see docs/llds/webhook-receiver.md § Jira adapter
    # "linear": LinearAdapter(),   ← see docs/llds/webhook-receiver.md § Linear adapter
}

PULL_ADAPTERS: dict[str, object] = {
    # "redis": RedisStreamsAdapter(),  ← see docs/llds/webhook-receiver.md § Redis Streams adapter
}
