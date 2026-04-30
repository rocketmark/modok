from __future__ import annotations
import asyncio
from pathlib import Path
from typing import Any

from modok.llm.errors import (
    LLMConfigError,
    LLMGatewayError,
    LLMResponseError,
    LLMUnavailableError,
)
from modok.llm.models import (
    KnownIssueSummary,
    MetadataProposal,
    SimilarityProposal,
    TicketParseResult,
)
from modok.quine.models import CustomerIssue


async def parse_ticket(
    raw_text: str,
    project_slug: str,
    backend: str = "local",
) -> TicketParseResult:
    raise NotImplementedError


async def propose_metadata(
    doc_path: Path,
    frontmatter: dict,
    missing_fields: list[str],
    backend: str = "local",
) -> MetadataProposal:
    raise NotImplementedError


async def propose_similarity(
    issue: CustomerIssue,
    candidates: list[KnownIssueSummary],
    backend: str = "local",
) -> list[SimilarityProposal]:
    raise NotImplementedError


async def _chat_completion(
    messages: list[dict],
    response_format: dict,
    backend: str,
    timeout: float,
) -> str:
    raise NotImplementedError


def _extract_json(raw: str) -> dict | None:
    """Attempt to extract a JSON object from raw text."""
    raise NotImplementedError


def _load_config() -> dict:
    raise NotImplementedError
