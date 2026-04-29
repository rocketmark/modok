from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from modok.ingestion.parser import ParsedDoc
from modok.ingestion.report import IngestionReport
from modok.ingestion.registry import Registry


NODE_WRITE_ORDER = [
    "Project",
    "Feature",
    "Module",
    "File",
    "DocSection",
    "KnownIssue",
    "Fix",
    "ResolutionEvent",
    "DiagnosticNote",
    "ErrorSignature",
    "CustomerIssue",
    "SimilarityMatch",
]


@dataclass
class IngestionContext:
    project_slug: str
    repo_root: Path
    registry: Registry
    fix_mode: bool = False
    report: IngestionReport = field(default_factory=IngestionReport)


def check_required_fields(doc: ParsedDoc, registry: Registry) -> list[str]:
    """Return list of error strings for missing required fields."""
    raise NotImplementedError


def validate_references(doc: ParsedDoc, registry: Registry) -> list[str]:
    """Validate frontmatter slug references against registries. Return warnings."""
    raise NotImplementedError


def validate_file_references(block: dict, repo_root: Path) -> list[str]:
    """Validate file_path references in a modok block exist on disk. Return warnings."""
    raise NotImplementedError


def process_modok_blocks(
    doc: ParsedDoc, ctx: IngestionContext
) -> list[dict]:
    """Process modok fenced blocks, return list of validated block dicts."""
    raise NotImplementedError


def score_fact(fact: Any, source: str) -> float:
    """Return confidence score for a fact given its source."""
    raise NotImplementedError


def fact_disposition(score: float) -> str:
    """Return 'write', 'write_pending', or 'propose' based on confidence band."""
    raise NotImplementedError


def build_doc_edges(doc: ParsedDoc, ctx: IngestionContext) -> None:
    """Write edges from DocSection nodes to Feature/Module/File nodes."""
    raise NotImplementedError


def ingest_fix_yaml(path: Path, ctx: IngestionContext) -> None:
    """Ingest a Fix YAML file into the graph."""
    raise NotImplementedError


def ingest_resolution_yaml(path: Path, ctx: IngestionContext) -> None:
    """Ingest a ResolutionEvent YAML file into the graph."""
    raise NotImplementedError


def check_working_tree(repo_root: Path) -> bool:
    """Return True if working tree is clean."""
    raise NotImplementedError


def ingest_doc(doc: ParsedDoc, ctx: IngestionContext) -> None:
    """Ingest a single parsed doc into the graph."""
    raise NotImplementedError


def apply_llm_proposals(proposals: list[dict], ctx: IngestionContext) -> None:
    """Apply approved LLM proposals to the graph (fix mode only)."""
    raise NotImplementedError


def user_approves(proposals: list[dict]) -> list[dict]:
    """Prompt user to approve/reject LLM proposals. Return approved subset."""
    raise NotImplementedError


def invoke_llm_gateway(doc: ParsedDoc, ctx: IngestionContext) -> list[dict]:
    """Call LLM gateway to generate proposals for a doc. Returns proposal list."""
    raise NotImplementedError


def run_ingestion(
    project_slug: str,
    ingestion_paths: list[str],
    repo_root: Path,
    fix_mode: bool = False,
) -> IngestionReport:
    """Top-level entry point. Discover, parse, ingest all docs under ingestion_paths."""
    raise NotImplementedError
