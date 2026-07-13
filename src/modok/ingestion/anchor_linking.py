"""Mechanical, LLM-free anchor linking — substring-matches a CustomerIssue's
raw_text against the project's registered error signatures and links only to
ErrorSignature nodes that already exist in the graph. See
docs/llds/standing-queries.md § Mechanical Anchor Linking."""
# @spec SQ-ANCH-001, SQ-ANCH-002, SQ-ANCH-003, SQ-ANCH-004, SQ-ANCH-005

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from modok.ingestion.errors import RegistryNotFoundError
from modok.ingestion.registry import Registry
from modok.quine.ids import idFrom as _idFrom


async def link_customer_issue_error_anchors(
    client: Any,
    project_slug: str,
    repo_root: Path,
    source_system: str,
    ticket_id: str,
    raw_text: str | None,
) -> list[str]:
    """Link a CustomerIssue to already-validated ErrorSignature nodes.

    Never invents an ErrorSignature node and never calls an LLM. Returns the
    list of normalized_error strings linked.
    """
    # @spec SQ-ANCH-004
    if not raw_text:
        return []

    try:
        registry = Registry(Path(repo_root))
    except RegistryNotFoundError as exc:
        # @spec SQ-ANCH-005
        print(f"anchor linking: {exc}", file=sys.stderr)
        return []

    matched: list[str] = []
    for normalized_error in registry.error_normalized_values():
        # @spec SQ-ANCH-001 — word-boundary, case-insensitive
        if not re.search(rf"\b{re.escape(normalized_error)}\b", raw_text, re.IGNORECASE):
            continue
        # @spec SQ-ANCH-002 — only link to nodes that already exist
        error_id = _idFrom("error", project_slug, normalized_error)
        if await client.node_exists(error_id):
            matched.append(normalized_error)

    # @spec SQ-ANCH-003 — reconcile the full current set in one call
    ci_id = _idFrom("customer-issue", project_slug, source_system, ticket_id)
    to_ids = [_idFrom("error", project_slug, e) for e in matched]
    await client.replace_edges(ci_id, "HAS_ERROR", to_ids)

    return matched
