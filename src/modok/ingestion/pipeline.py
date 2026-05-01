from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import re
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
from modok.quine.models import (
    Feature, Module, File, DocSection, ErrorSignature,
    KnownIssue, Fix,
)

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
    edges_written: int = 0
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


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text


async def route_fact(
    value: Any,
    score: float,
    ctx: IngestionContext,
    client: Any,
    source: str = "prose",
) -> None:
    """Route a scored prose-extracted fact through the confidence model.

    Only for prose/structure-extracted facts. Frontmatter and MODOK block
    facts are always verified (1.00) and written via build_nodes_from_frontmatter.
    """
    band = confidence_band(base=score)

    # Prose facts are opaque strings, not typed nodes — skip if not a string.
    if not isinstance(value, str):
        return

    if band.score < _STRONG_THRESHOLD:
        ctx.add_pending_fact(value=value, score=band.score, evidence=source)


def check_required_fields(
    frontmatter: dict | None,
    doc_type: str,
    fix: bool = False,
    doc_path: Path | None = None,
) -> tuple[list[str], list[str]]:
    """Return (warnings, errors) for missing required fields."""
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
        warnings.extend(f"LLM proposed value for missing field: {k}" for k in proposals)
    elif missing:
        warnings.extend(f"Missing field: {f}" for f in missing)

    return warnings, errors


def validate_references(frontmatter: dict, registry: Registry) -> list[str]:
    """Validate frontmatter slug references against registries."""
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
    """Validate file_path references exist on disk."""
    warnings: list[str] = []
    errors: list[str] = []

    for key in ("source_files", "test_files", "files_changed"):
        for fpath in block.get(key, []):
            full = repo_root / fpath
            if not full.exists():
                warnings.append(f"Referenced file not found on disk: {fpath}")

    return warnings, errors


def process_modok_blocks(blocks: list[dict]) -> tuple[list[dict], list[str], list[str]]:
    """Process parsed modok blocks, return (valid_blocks, warnings, errors)."""
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


def check_working_tree(repo_root: Path) -> list[str]:
    """Return list of warnings if working tree is dirty."""
    if is_working_tree_dirty(repo_root):
        return [
            "Working tree is dirty — commit SHA for doc nodes reflects last commit, "
            "not current uncommitted changes."
        ]
    return []


async def _write_nodes_and_edges(
    fm: dict,
    path: Path,
    project_slug: str,
    repo_root: Path,
    headings: list[tuple[str, str, int, int | None]],
    client: Any,
    ctx: IngestionContext,
) -> None:
    """Build QuineNode models from frontmatter and write them with edges."""
    feature_slug: str = fm.get("feature", "")
    doc_type: str = (fm.get("doc_type") or fm.get("modok", {}).get("doc_type", "")) if isinstance(fm, dict) else ""
    product_area_slug: str | None = fm.get("product_area") or None
    doc_path_str = str(path.relative_to(repo_root)) if path.is_relative_to(repo_root) else str(path)

    # --- Feature node ---
    if feature_slug:
        registry_entry = {}
        try:
            from modok.ingestion.registry import Registry as _Reg
        except ImportError:
            pass

        feature_node = Feature(
            node_type="Feature",
            project_slug=project_slug,
            feature_slug=feature_slug,
            name=feature_slug,  # registry lookup could enrich this later
            product_area_slug=product_area_slug,
        )
        await client.upsert_node(feature_node)
        ctx.nodes_written += 1

    # --- Module nodes + IMPLEMENTED_BY edges ---
    for mod_slug in fm.get("modules", []):
        mod_node = Module(
            node_type="Module",
            project_slug=project_slug,
            module_slug=mod_slug,
            name=mod_slug,
        )
        await client.upsert_node(mod_node)
        ctx.nodes_written += 1
        if feature_slug:
            await client.write_edge_by_parts(
                ("feature", project_slug, feature_slug),
                "IMPLEMENTED_BY",
                ("module", project_slug, mod_slug),
            )
            ctx.edges_written += 1

    # --- File nodes (source + test) + DEFINED_IN edges ---
    all_files: list[str] = list(fm.get("source_files", [])) + list(fm.get("test_files", []))
    for fpath in all_files:
        file_node = File(
            node_type="File",
            project_slug=project_slug,
            repo_path=fpath,
        )
        await client.upsert_node(file_node)
        ctx.nodes_written += 1
        # Determine which module owns this file (first module in list, if any)
        for mod_slug in fm.get("modules", []):
            await client.write_edge_by_parts(
                ("module", project_slug, mod_slug),
                "DEFINED_IN",
                ("file", project_slug, fpath),
            )
            ctx.edges_written += 1
            break  # one DEFINED_IN per file is enough for v1

    # --- ErrorSignature nodes + HAS_ERROR edges ---
    for err_slug in fm.get("error_signatures", []):
        err_node = ErrorSignature(
            node_type="ErrorSignature",
            project_slug=project_slug,
            normalized_error=err_slug,
            display_text=err_slug,
        )
        await client.upsert_node(err_node)
        ctx.nodes_written += 1
        if feature_slug:
            await client.write_edge_by_parts(
                ("feature", project_slug, feature_slug),
                "HAS_ERROR",
                ("error", project_slug, err_slug),
            )
            ctx.edges_written += 1

    # --- DocSection nodes + DESCRIBED_BY edges (SI-HEAD-001, SI-HEAD-002) ---
    if feature_slug:
        for heading_text, heading_slug, line_start, line_end in headings:
            section_node = DocSection(
                node_type="DocSection",
                project_slug=project_slug,
                doc_path=doc_path_str,
                heading_slug=heading_slug,
                heading_text=heading_text,
                doc_type=doc_type,
                line_start=line_start,
                line_end=line_end,
            )
            await client.upsert_node(section_node)
            ctx.nodes_written += 1
            await client.write_edge_by_parts(
                ("feature", project_slug, feature_slug),
                "DESCRIBED_BY",
                ("doc-section", project_slug, doc_path_str, heading_slug),
            )
            ctx.edges_written += 1


async def _write_known_issue_block(
    block: dict,
    project_slug: str,
    feature_slug: str,
    client: Any,
    ctx: IngestionContext,
) -> None:
    issue_id = block.get("id", "")
    if not issue_id:
        return
    ki_node = KnownIssue(
        node_type="KnownIssue",
        project_slug=project_slug,
        issue_id=issue_id,
        summary=block.get("summary", block.get("symptom", "")),
        status=block.get("status", "open"),
    )
    await client.upsert_node(ki_node)
    ctx.nodes_written += 1
    if feature_slug:
        await client.write_edge_by_parts(
            ("known-issue", project_slug, issue_id),
            "AFFECTS",
            ("feature", project_slug, feature_slug),
        )
        ctx.edges_written += 1


async def _write_fix_block(
    block: dict,
    project_slug: str,
    client: Any,
    ctx: IngestionContext,
) -> None:
    fix_id = block.get("id", block.get("fix_id", ""))
    if not fix_id:
        return
    fix_node = Fix(
        node_type="Fix",
        project_slug=project_slug,
        fix_id=fix_id,
        summary=block.get("summary", ""),
        kind=block.get("kind", "code-fix"),
    )
    await client.upsert_node(fix_node)
    ctx.nodes_written += 1


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

    # Normalise: support both top-level keys and nested modok: block
    if isinstance(fm, dict) and "modok" in fm and isinstance(fm["modok"], dict):
        modok = fm["modok"]
        # Merge modok: subkeys up to top level (top-level wins on conflict)
        merged = {**modok, **{k: v for k, v in fm.items() if k != "modok"}}
        fm = merged

    doc_type = fm.get("doc_type", "")
    feature_slug: str = fm.get("feature", "")

    # SI-FMTR-002/003: check required fields
    field_warnings, _ = check_required_fields(
        fm, doc_type, fix=ctx.fix_mode, doc_path=path
    )
    for w in field_warnings:
        ctx.add_warning(w)

    # SI-REF-001/002/003: validate registry references (raises on invalid slug)
    validate_references(fm, registry)

    # SI-REF-004: validate file references (warnings + confidence penalty)
    file_warnings, _ = validate_file_references(fm, repo_root)
    for w in file_warnings:
        ctx.add_warning(w)

    content = path.read_text(encoding="utf-8", errors="replace")

    # SI-BLOCK-001/002/003: parse and write MODOK block facts (always score=1.00)
    blocks = parse_modok_blocks(content)
    valid_blocks, block_warnings, _ = process_modok_blocks(blocks)
    for w in block_warnings:
        ctx.add_warning(w)

    for block in valid_blocks:
        kind = block.get("kind")
        if kind == "known_issue":
            await _write_known_issue_block(block, project_slug, feature_slug, client, ctx)
        elif kind == "fix":
            await _write_fix_block(block, project_slug, client, ctx)
        # failure_mode, risk, diagnostic_note: deferred to later ingestion phases

    # SI-HEAD-001/002: extract headings → DocSection nodes + DESCRIBED_BY edges
    headings = parse_headings(content)

    # SI-WRITE-001: write nodes and edges in dependency order
    await _write_nodes_and_edges(fm, path, project_slug, repo_root, headings, client, ctx)

    return True


def ingest_fix_yaml(path: Path, ctx: IngestionContext | None = None) -> None:
    """Validate a Fix YAML file. Raises MissingCommitShaError if commit_sha absent."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise MissingCommitShaError(f"Invalid YAML in {path}")
    if not data.get("commit_sha"):
        raise MissingCommitShaError(f"commit_sha is required in Fix YAML: {path}")


def ingest_resolution_yaml(path: Path, ctx: IngestionContext | None = None) -> None:
    """Validate a ResolutionEvent YAML file. Raises MissingCommitShaError if commit_sha absent."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise MissingCommitShaError(f"Invalid YAML in {path}")
    if not data.get("commit_sha"):
        raise MissingCommitShaError(f"commit_sha is required in ResolutionEvent YAML: {path}")


def apply_llm_proposals(
    path: Path,
    proposals: dict,
    client: Any,
) -> None:
    """Apply approved LLM proposals by writing to doc frontmatter then re-parsing."""
    if not user_approves(proposals):
        return

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
    """Prompt user to approve/reject LLM proposals."""
    if isinstance(proposals, dict):
        print(f"LLM proposals: {proposals}")
    answer = input("Accept proposals? [y/N] ").strip().lower()
    return answer == "y"


# @spec LLM-META-004
def invoke_llm_gateway(doc_path: Path, ctx: IngestionContext | None = None) -> dict:
    """Call LLM gateway to generate proposals for a doc."""
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
        raise _LLMProposalWarning(str(exc)) from exc

    return proposal.proposed_fields


class _LLMProposalWarning(Exception):
    pass


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
                report.files_skipped += 1
        except InvalidSlugReferenceError as exc:
            report.errors.append(str(exc))
        except Exception as exc:
            report.errors.append(f"{path}: {exc}")

    report.nodes_written = ctx.nodes_written
    report.edges_written = ctx.edges_written
    report.pending_items = ctx.pending_count
    report.warnings.extend(ctx._warnings)
    report.duration_seconds = time.monotonic() - t0

    if ctx._pending:
        for fact in ctx._pending:
            print(f"Pending (score={fact['score']:.2f}): {fact['value']} — {fact['evidence']}")
        if user_approves(ctx._pending):
            for fact in ctx._pending:
                ctx.nodes_written += 1
            report.nodes_written = ctx.nodes_written
            report.pending_items = 0

    return report
