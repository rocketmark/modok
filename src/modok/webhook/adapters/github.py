from __future__ import annotations

import hashlib
import hmac

from modok.ingestion.github import ticket_kind_from_labels
from modok.webhook.errors import WebhookAuthError
from modok.webhook.models import CustomerIssueData, FixData, IngestEvent, WebhookConfig

_ISSUE_ACTIONS = {"opened", "edited", "labeled", "closed", "reopened"}


class GitHubAdapter:
    # @spec WH-GH-001, WH-GH-002, WH-GH-003, WH-GH-004, WH-GH-005,
    #        WH-GH-006, WH-GH-007, WH-GH-008, WH-GH-009, WH-GH-010

    def verify_request(self, request: object, config: WebhookConfig) -> None:
        # @spec WH-GH-001, WH-PUSH-009
        # Raw bytes must be read by the router before calling here; they are
        # stored on request.body_bytes so HMAC is computed on the original wire bytes.
        # Headers are lowercased (HTTP/1.1 convention; FastAPI normalises on receipt).
        headers: dict = getattr(request, "headers", {})
        sig_header: str = (
            headers.get("x-hub-signature-256")
            or headers.get("X-Hub-Signature-256")
            or ""
        )
        if not sig_header:
            raise WebhookAuthError("Missing X-Hub-Signature-256 header")
        body_bytes: bytes = getattr(request, "body_bytes", b"")
        expected = "sha256=" + hmac.new(
            config.github_secret.encode(), body_bytes, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, sig_header):
            raise WebhookAuthError("Invalid HMAC signature")

    def normalize_event(self, payload: dict, event_type: str) -> IngestEvent | None:
        project_slug = getattr(payload, "__project_slug__", "")
        if event_type == "ping":
            # @spec WH-GH-002
            return IngestEvent(kind="skip", project_slug=project_slug, data=None)

        if event_type == "issues":
            # @spec WH-GH-003, WH-GH-007
            action = payload.get("action", "")
            if action not in _ISSUE_ACTIONS:
                return IngestEvent(kind="skip", project_slug=project_slug, data=None)
            issue = payload.get("issue", {})
            label_names = [label.get("name", "") for label in issue.get("labels", [])]
            return IngestEvent(
                kind="customer_issue",
                project_slug=project_slug,
                data=CustomerIssueData(
                    ticket_id=str(issue.get("number", "")),
                    summary=issue.get("title", ""),
                    raw_text=issue.get("body") or "",
                    status=issue.get("state", "open"),
                    source_system="github",
                    ticket_kind=ticket_kind_from_labels(label_names),
                ),
            )

        if event_type == "pull_request":
            # @spec WH-GH-004, WH-GH-005, WH-GH-008
            action = payload.get("action", "")
            merged = payload.get("merged", False)
            if action == "closed" and merged:
                pr = payload.get("pull_request", {})
                return IngestEvent(
                    kind="fix",
                    project_slug=project_slug,
                    data=FixData(
                        fix_id="gh-" + str(pr.get("number", "")),
                        summary=pr.get("title", ""),
                    ),
                )
            return IngestEvent(kind="skip", project_slug=project_slug, data=None)

        # @spec WH-GH-006
        return IngestEvent(kind="skip", project_slug=project_slug, data=None)
