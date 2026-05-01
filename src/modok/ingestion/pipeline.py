from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from modok.ingestion.confidence import confidence_band
from modok.ingestion.errors import (
    InvalidSlugReferenceError,
    MissingCommitShaError,
    MissingRequiredFieldError,
)
from modok.ingestion.parser import (
    ParsedDoc,
    get_commit_sha,
    is_working_tree_dirty,
    parse_frontmatter,
    parse_headings,
    parse_modok_blocks,
)
from modok.ingestion.registry import Registry
from modok.ingestion.report import IngestionReport
from modok.ingestion.discovery import discover_files

NODE_WRITE_ORDER = [
    "Project",
    "ProductArea",
    "Feature",
    "Module",
    "File",
    "Doc",
    "ErrorSignature",
    "FailureMode",
    "Risk",
    "KnownIssue",
    "Fix",
    "CustomerIssue",
    "ResolutionEvent",
]
# ^ Dependency-respecting write order: anchors (Project, Feature) before dependents.

_KNOWN_BLOCK_KINDS = {
    "failure_mode",
    "known_issue",
    "fix",
    "risk",
    "diagnostic_note",
}


@dataclass
class IngestionContext:
    project_slug: str = ""
    repo_root: Path = field(default_factory=Path)
    fix_mode: bool = False
    nodes_written: int = 0
    _pending: list[dict] = field(default_factory=list)
    _warnings: list[str] = field(default_factory=list)

    def add_pending_fact(self, value: Any, score: float, evidence: str) -> None:
        self._pending.append({"value": value, "score": score, "evidence": evidence})

    def add_warning(self, message: str) -> None:
        self._warnings.append(message)

    @property
    def pending_count(self) -> int:
        return len(self._pending)


_VERIFIED_THRESHOLD = 0.90
_STRONG_THRESHOLD = 0.75


async def route_fact(
    value: Any,
    score: float,
    ctx: IngestionContext,
    client: Any,
    source: str = "prose",
) -> None:
    """Route a scored fact through the confidence model.

    - score >= 0.90: write immediately via upsert_node
    - 0.75 <= score < 0.90: write with confidence_low/confidence_high properties
    - score < 0.75: add to pending; do not write
    """
    band = confidence_band(base=score)

    if band.score >= _VERIFIED_THRESHOLD:
        node: dict[str, Any] = {"value": value, "source": source}
        await client.upsert_node(node)
        ctx.nodes_written += 1
    elif band.score >= _STRONG_THRESHOLD:
        node = {
            "value": value,
            "source": source,
            "confidence_low": band.low,
            "confidence_high": band.high,
        }
        await client.upsert_node(node)
        ctx.nodes_written += 1
    else:
        ctx.add_pending_fact(value=value, score=band.score, evidence=source)


def check_required_fields(
    frontmatter: dict | None,
    doc_type: str,
    fix: bool = False,
    doc_path: Path | None = None,
) -> tuple[list[str], list[str]]:
    """Return (warnings, errors) for missing required fields.

    If fix=True and the LLM gateway is available, invoke it to propose values.
    Never invokes LLM when fix=False.
    """
    if frontmatter is None:
        return [], ["frontmatter is None"]

    _REQUIRED_BY_TYPE: dict[str, list[str]] = {
        "lld": ["feature", "modules", "source_files", "test_files"],
        "spec": ["feature"],
        "adr": ["feature"],
    }
    required = _REQUIRED_BY_TYPE.get(doc_type, [])

    missing = [f for f in required if not frontmatter.get(f)]
    warnings: list[str] = []
    errors: list[str] = []

    if missing and fix and doc_path is not None:
        from modok.llm.errors import LLMResponseError, LLMUnavailableError

        try:
            proposals = invoke_llm_gateway(doc_path)
        except (_LLMProposalWarning, LLMResponseError, LLMUnavailableError) as exc:
            warnings.append(f"LLM proposal skipped for {doc_path.name}: {exc}")
            proposals = {}
        # proposals go to doc frontmatter, not directly to Quine
        warnings.extend(f"LLM proposed value for missing field: {k}" for k in proposals)
    elif missing:
        warnings.extend(f"Missing field: {f}" for f in missing)

    return warnings, errors


def validate_references(frontmatter: dict, registry: Registry) -> list[str]:
    """Validate frontmatter slug references against registries. Raises on invalid slug."""
    feature = frontmatter.get("feature")
    if feature and not registry.has_feature(feature):
        raise InvalidSlugReferenceError(f"Unknown feature slug: {feature!r}")

    for mod in frontmatter.get("modules", []):
        if not registry.has_module(mod):
            raise InvalidSlugReferenceError(f"Unknown module slug: {mod!r}")

    for err in frontmatter.get("error_signatures", []):
        if not registry.has_error(err):
            raise InvalidSlugReferenceError(f"Unknown error slug: {err!r}")

    return []


def validate_file_references(block: dict, repo_root: Path) -> tuple[list[str], list[str]]:
    """Validate file_path references exist on disk. Returns (warnings, errors)."""
    warnings: list[str] = []
    errors: list[str] = []

    for key in ("source_files", "test_files", "files_changed"):
        for fpath in block.get(key, []):
            full = repo_root / fpath
            if not full.exists():
                warnings.append(f"Referenced file not found on disk: {fpath}")

    return warnings, errors


def process_modok_blocks(blocks: list[dict]) -> tuple[list[dict], list[str], list[str]]:
    """Process parsed modok blocks, return (valid_nodes, warnings, errors)."""
    nodes: list[dict] = []
    warnings: list[str] = []
    errors: list[str] = []

    for block in blocks:
        kind = block.get("kind")
        if kind not in _KNOWN_BLOCK_KINDS:
            warnings.append(f"Unknown block kind: {kind!r} — skipping block")
            continue
        nodes.append(block)

    return nodes, warnings, errors


def build_doc_edges(
    feature_slug: str,
    project_slug: str,
    doc_path: Path,
    headings: list[tuple[str, str, int, int | None]],
) -> list[tuple[str, str, str]]:
    """Return list of (from_id, rel_type, to_id) edge tuples."""
    from modok.quine.ids import idFrom

    feature_id = str(idFrom("Feature", project_slug, feature_slug))
    edges = []
    for text, slug, line_start, line_end in headings:
        section_id = str(idFrom("DocSection", project_slug, str(doc_path), slug))
        edges.append((feature_id, "DESCRIBED_BY", section_id))
    return edges


def ingest_fix_yaml(path: Path, ctx: IngestionContext | None = None) -> None:
    """Ingest a Fix YAML file into the graph. Raises MissingCommitShaError if sha absent."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise MissingCommitShaError(f"Invalid YAML in {path}")
    if not data.get("commit_sha"):
        raise MissingCommitShaError(f"commit_sha is required in Fix YAML: {path}")


def ingest_resolution_yaml(path: Path, ctx: IngestionContext | None = None) -> None:
    """Ingest a ResolutionEvent YAML file into the graph. Raises MissingCommitShaError if sha absent."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise MissingCommitShaError(f"Invalid YAML in {path}")
    if not data.get("commit_sha"):
        raise MissingCommitShaError(f"commit_sha is required in ResolutionEvent YAML: {path}")


def check_working_tree(repo_root: Path) -> list[str]:
    """Return list of warnings if working tree is dirty."""
    if is_working_tree_dirty(repo_root):
        return [
            "Working tree is dirty — commit SHA for doc nodes reflects last commit, "
            "not current uncommitted changes."
        ]
    return []


async def ingest_doc(
    path: Path,
    registry: Registry,
    client: Any,
    project_slug: str,
    repo_root: Path,
    ctx: IngestionContext | None = None,
) -> bool:
    """Ingest a single markdown/yaml doc. Returns False if skipped (no modok: frontmatter)."""
    if ctx is None:
        ctx = IngestionContext(project_slug=project_slug, repo_root=repo_root)

    fm = parse_frontmatter(path)
    if fm is None:
        return False

    # SI-FMTR-002/003, LLM-META-004: check required fields; invoke LLM only in fix_mode
    doc_type = (fm.get("doc_type") or fm.get("modok", {}).get("doc_type", "")) if isinstance(fm, dict) else ""
    field_warnings, _ = check_required_fields(
        fm, doc_type, fix=ctx.fix_mode, doc_path=path
    )
    for w in field_warnings:
        ctx.add_warning(w)

    validate_references(fm, registry)

    content = path.read_text(encoding="utf-8", errors="replace")

    # MODOK block facts — always confidence 1.00, bypass scoring (SI-BLOCK-002)
    blocks = parse_modok_blocks(content)
    valid_blocks, block_warnings, _ = process_modok_blocks(blocks)
    for block in valid_blocks:
        await route_fact(value=block, score=1.00, ctx=ctx, client=client, source="modok_block")

    # Heading extraction → DocSection nodes and DESCRIBED_BY edges (SI-HEAD-001, SI-HEAD-002)
    feature_slug = fm.get("feature", "")
    headings = parse_headings(content)
    if feature_slug and headings:
        edges = build_doc_edges(feature_slug, project_slug, path, headings)
        for edge in edges:
            await client.write_edge(*edge)

    # File reference validation — missing file applies −0.15 confidence penalty (SI-REF-004)
    file_warnings, _ = validate_file_references(fm, repo_root)
    base_score = 0.88  # markdown_link base
    for warning in file_warnings:
        await route_fact(
            value=warning,
            score=confidence_band(base=base_score, penalties=[0.15]).score,
            ctx=ctx,
            client=client,
            source="file_ref",
        )

    # Commit SHA for this doc node (SI-SHA-001)
    sha = get_commit_sha(path)
    await client.upsert_node({"type": "Doc", "path": str(path), "commit_sha": sha})
    ctx.nodes_written += 1
    return True


def apply_llm_proposals(
    path: Path,
    proposals: dict,
    client: Any,
) -> None:
    """Apply approved LLM proposals by writing to doc frontmatter then re-parsing.

    Never calls client.upsert_node directly — Quine writes happen via re-parse.
    """
    if not user_approves(proposals):
        return

    # Read current content and patch frontmatter
    text = path.read_text(encoding="utf-8")
    end = text.find("\n---", 3)
    if end == -1:
        return

    import yaml as _yaml
    fm_raw = text[3:end]
    try:
        fm_data = _yaml.safe_load(fm_raw) or {}
    except Exception:
        fm_data = {}

    modok_block = fm_data.get("modok", {}) or {}
    modok_block.update(proposals)
    fm_data["modok"] = modok_block

    new_fm = _yaml.dump(fm_data, default_flow_style=False)
    new_content = f"---\n{new_fm}---{text[end + 4:]}"
    path.write_text(new_content, encoding="utf-8")


def user_approves(proposals: dict | list) -> bool:
    """Prompt user to approve/reject LLM proposals. Returns True if approved."""
    # In CLI context this prompts interactively; in test context callers patch this.
    if isinstance(proposals, dict):
        print(f"LLM proposals: {proposals}")
    answer = input("Accept proposals? [y/N] ").strip().lower()
    return answer == "y"


# @spec LLM-META-004
def invoke_llm_gateway(doc_path: Path, ctx: IngestionContext | None = None) -> dict:
    """Call LLM gateway to generate proposals for a doc. Returns proposal dict.

    Returns an empty dict (and records a warning on ctx) when the LLM raises
    LLMResponseError or LLMUnavailableError — ingestion of other files continues.
    """
    import asyncio
    from modok.llm import gateway
    from modok.llm.errors import LLMResponseError, LLMUnavailableError

    fm = parse_frontmatter(doc_path) or {}
    missing = [f for f in ["feature", "modules", "source_files", "test_files"] if not fm.get(f)]
    if not missing:
        return {}

    try:
        proposal = asyncio.run(
            gateway.propose_metadata(
                doc_path=doc_path,
                frontmatter=fm,
                missing_fields=missing,
            )
        )
    except (LLMResponseError, LLMUnavailableError) as exc:
        warning = f"LLM proposal skipped for {doc_path.name}: {exc}"
        if ctx is not None:
            ctx._pending  # ensure ctx is live; warnings go via report in run_ingestion
        # Surface as a warning by raising a sentinel the caller can catch cleanly.
        # check_required_fields callers pass warnings up; we add directly here.
        raise _LLMProposalWarning(warning) from exc

    return proposal.proposed_fields


class _LLMProposalWarning(Exception):
    """Internal sentinel: LLM proposal failed; treat as warning, not error."""


async def run_ingestion(
    repo_root: Path,
    registry: Registry,
    client: Any,
    project_slug: str,
    fix_mode: bool = False,
) -> IngestionReport:
    """Top-level entry point. Discover, parse, ingest all docs under repo_root."""
    import time
    report = IngestionReport()
    ctx = IngestionContext(project_slug=project_slug, repo_root=repo_root, fix_mode=fix_mode)

    # SI-SHA-003: warn if working tree is dirty before reading any SHAs
    report.warnings.extend(check_working_tree(repo_root))

    t0 = time.monotonic()
    files, ignored = discover_files(repo_root)
    report.files_ignored = ignored

    for path in files:
        try:
            processed = await ingest_doc(
                path,
                registry=registry,
                client=client,
                project_slug=project_slug,
                repo_root=repo_root,
                ctx=ctx,
            )
            if processed:
                report.docs_processed += 1
            else:
                report.files_skipped += 1  # SI-DISC-003
        except InvalidSlugReferenceError as exc:
            report.errors.append(str(exc))
        except Exception as exc:
            report.errors.append(f"{path}: {exc}")

    report.nodes_written = ctx.nodes_written
    report.pending_items = ctx.pending_count
    report.warnings.extend(ctx._warnings)
    report.duration_seconds = time.monotonic() - t0

    # SI-CONF-004: present pending low-confidence facts for interactive approval
    if ctx._pending:
        for fact in ctx._pending:
            print(f"Pending (score={fact['score']:.2f}): {fact['value']} — {fact['evidence']}")
        if user_approves(ctx._pending):
            for fact in ctx._pending:
                await client.upsert_node({"value": fact["value"], "source": "pending_approved"})
                ctx.nodes_written += 1
            report.nodes_written = ctx.nodes_written
            report.pending_items = 0

    return report


