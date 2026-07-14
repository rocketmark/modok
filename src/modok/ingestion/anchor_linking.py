"""Anchor linking for CustomerIssue nodes — two mechanical, LLM-free linkers
(errors via substring match, features via token match) plus an LLM fallback
classifier that only ever runs when both mechanical linkers find nothing. See
docs/llds/standing-queries.md § Mechanical Anchor Linking,
§ LLM Fallback Anchor Classification."""
# @spec SQ-ANCH-001, SQ-ANCH-002, SQ-ANCH-003, SQ-ANCH-004, SQ-ANCH-005,
#       SQ-ANCH-006, SQ-ANCH-007, SQ-ANCH-008, SQ-ANCH-009, SQ-ANCH-010,
#       SQ-LLMANCH-001, SQ-LLMANCH-003, SQ-LLMANCH-004, SQ-LLMANCH-005,
#       SQ-LLMANCH-006, SQ-LLMANCH-007

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from modok.ingestion.errors import RegistryNotFoundError
from modok.ingestion.registry import Registry
from modok.llm import gateway
from modok.llm.errors import LLMGatewayError, LLMUnavailableError
from modok.quine.ids import idFrom as _idFrom
from modok.text_utils import extract_text_tokens, tokenize


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


# @spec SQ-ANCH-008, SQ-ANCH-009, SQ-ANCH-010
async def link_customer_issue_feature_anchors(
    client: Any,
    project_slug: str,
    repo_root: Path,
    source_system: str,
    ticket_id: str,
    raw_text: str | None,
) -> list[str]:
    """Link a CustomerIssue to already-validated Feature nodes via token overlap.

    Never invents a Feature node and never calls an LLM. Returns the list of
    feature slugs linked.
    """
    # @spec SQ-ANCH-008
    if not raw_text:
        return []

    try:
        registry = Registry(Path(repo_root))
    except RegistryNotFoundError as exc:
        print(f"anchor linking: {exc}", file=sys.stderr)
        return []

    text_tokens = extract_text_tokens(raw_text)

    matched: list[str] = []
    for slug, name in registry.feature_names().items():
        # @spec SQ-ANCH-008 — token overlap, not exact substring
        slug_tokens = tokenize(slug) | tokenize(name)
        if not slug_tokens or not (slug_tokens & text_tokens):
            continue
        # @spec SQ-ANCH-009 — only link to nodes that already exist
        feature_id = _idFrom("feature", project_slug, slug)
        if await client.node_exists(feature_id):
            matched.append(slug)

    # @spec SQ-ANCH-010 — reconcile the full current set in one call
    ci_id = _idFrom("customer-issue", project_slug, source_system, ticket_id)
    to_ids = [_idFrom("feature", project_slug, s) for s in matched]
    await client.replace_edges(ci_id, "AFFECTS", to_ids)

    return matched


# @spec SQ-LLMANCH-001, SQ-LLMANCH-003, SQ-LLMANCH-004, SQ-LLMANCH-005,
#       SQ-LLMANCH-006, SQ-LLMANCH-007
async def classify_customer_issue_anchors(
    client: Any,
    project_slug: str,
    repo_root: Path,
    source_system: str,
    ticket_id: str,
    raw_text: str | None,
    backend: str = "local",
) -> None:
    """LLM fallback anchor classification.

    Only meant to be called by a call site when both mechanical linkers
    (link_customer_issue_error_anchors, link_customer_issue_feature_anchors)
    found nothing for this CustomerIssue (SQ-LLMANCH-001, SQ-LLMANCH-002 — the
    gating decision itself lives at the call site, not in this function).
    Calls the LLM Gateway's parse_ticket with the project's registry context,
    validates the result against the registry and existing graph nodes — the
    same never-invent-a-node discipline as the mechanical linkers — and
    persists any resulting HAS_ERROR/AFFECTS edges. Never raises: LLM
    unavailability, a rejected response, or a missing registry all degrade to
    writing no anchors, not an ingestion failure.
    """
    # @spec SQ-LLMANCH-001
    if not raw_text:
        return

    try:
        registry = Registry(Path(repo_root))
    except RegistryNotFoundError as exc:
        # @spec SQ-LLMANCH-007
        print(f"anchor linking (llm fallback): {exc}", file=sys.stderr)
        return

    feature_slugs = registry.feature_slugs()
    module_slugs = registry.module_slugs()
    valid_slugs = feature_slugs + module_slugs

    try:
        result = await gateway.parse_ticket(
            raw_text,
            project_slug,
            backend=backend,
            valid_slugs=valid_slugs,
            feature_slugs=feature_slugs,
            module_slugs=module_slugs,
            feature_descriptions=registry.feature_descriptions(),
            module_descriptions=registry.module_descriptions(),
            module_elements=registry.module_elements(),
            module_source_files=registry.all_module_source_files(),
        )
    except LLMUnavailableError as exc:
        # @spec SQ-LLMANCH-006
        print(f"anchor linking (llm fallback): LLM unavailable: {exc}", file=sys.stderr)
        return
    except LLMGatewayError as exc:
        # @spec SQ-LLMANCH-006
        print(f"anchor linking (llm fallback): LLM rejected response: {exc}", file=sys.stderr)
        return

    ci_id = _idFrom("customer-issue", project_slug, source_system, ticket_id)

    # @spec SQ-LLMANCH-003 — AFFECTS targets are Feature nodes only, never Module
    matched_features: list[str] = []
    for slug in result.feature_slugs:
        if slug not in feature_slugs:
            continue
        feature_id = _idFrom("feature", project_slug, slug)
        if await client.node_exists(feature_id):
            matched_features.append(slug)
    # @spec SQ-LLMANCH-005 — reconcile once, even when the matched set is empty
    await client.replace_edges(
        ci_id, "AFFECTS", [_idFrom("feature", project_slug, s) for s in matched_features]
    )

    # @spec SQ-LLMANCH-004 — validate against the registry; parse_ticket does
    # not pre-filter error_signatures the way it does feature_slugs.
    registered_errors = set(registry.error_normalized_values())
    matched_errors: list[str] = []
    for normalized_error in result.error_signatures:
        if normalized_error not in registered_errors:
            continue
        error_id = _idFrom("error", project_slug, normalized_error)
        if await client.node_exists(error_id):
            matched_errors.append(normalized_error)
    # @spec SQ-LLMANCH-005
    await client.replace_edges(
        ci_id, "HAS_ERROR", [_idFrom("error", project_slug, e) for e in matched_errors]
    )
