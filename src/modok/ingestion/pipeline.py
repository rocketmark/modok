from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import os
import re
import sys
import uuid
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
from modok.llm.gateway import propose_metadata
from modok.ingestion.verifier import verify_proposal, RejectedField
from modok.quine.models import (
    Doc, Feature, Module, File, DocSection, ErrorSignature,
    KnownIssue, Fix,
)

NODE_WRITE_ORDER = [
    "Project",
    "ProductArea",
    "Feature",
    "Module",
    "File",
    "Doc",
    "Commit",
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

    if missing:
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
    registry: Any = None,
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
    all_mod_slugs: list[str] = fm.get("modules", [])
    for fpath in all_files:
        file_node = File(
            node_type="File",
            project_slug=project_slug,
            repo_path=fpath,
        )
        await client.upsert_node(file_node)
        ctx.nodes_written += 1
        # Use registry to find which module(s) specifically own this file.
        # Fall back to all modules listed on the feature if registry unavailable.
        if registry is not None:
            owning_mods = registry.modules_covering_path(fpath)
            if not owning_mods:
                owning_mods = all_mod_slugs[:1]
        else:
            owning_mods = all_mod_slugs[:1]
        for mod_slug in owning_mods:
            await client.write_edge_by_parts(
                ("module", project_slug, mod_slug),
                "DEFINED_IN",
                ("file", project_slug, fpath),
            )
            ctx.edges_written += 1

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

    # --- File node for the doc itself + DEFINED_IN edge from module ---
    # Docs are files too: surface them in retrieval traversal and commit history.
    if feature_slug:
        doc_file_node = File(
            node_type="File",
            project_slug=project_slug,
            repo_path=doc_path_str,
        )
        await client.upsert_node(doc_file_node)
        ctx.nodes_written += 1
        doc_mod_slugs = list(fm.get("modules", []))
        if not doc_mod_slugs and registry is not None:
            doc_mod_slugs = registry.modules_for_feature(feature_slug)
        for mod_slug in doc_mod_slugs:
            await client.write_edge_by_parts(
                ("module", project_slug, mod_slug),
                "DEFINED_IN",
                ("file", project_slug, doc_path_str),
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
    doc_record: Any = None,
) -> bool:
    """Ingest a single markdown/yaml doc. Returns False if skipped (no frontmatter and no DocRecord)."""
    if ctx is None:
        ctx = IngestionContext(project_slug=project_slug, repo_root=repo_root)

    if doc_record is not None:
        # Registry-driven path: build fm from DocRecord, skip frontmatter checks
        fm: dict = {
            "doc_type": doc_record.doc_type,
            "feature": doc_record.feature or "",
            "modules": list(doc_record.modules),
            "source_files": list(doc_record.source_files),
            "test_files": list(doc_record.test_files),
        }
    else:
        # Legacy: parse frontmatter from file
        fm = parse_frontmatter(path)
        if fm is None:
            return False

        # Normalise: support both top-level keys and nested modok: block
        if isinstance(fm, dict) and "modok" in fm and isinstance(fm["modok"], dict):
            modok = fm["modok"]
            merged = {**modok, **{k: v for k, v in fm.items() if k != "modok"}}
            fm = merged

        # SI-FMTR-002/003: check required fields
        field_warnings, _ = check_required_fields(fm, fm.get("doc_type", ""))
        for w in field_warnings:
            ctx.add_warning(w)

        # SI-REF-001/002/003: validate registry references (raises on invalid slug)
        validate_references(fm, registry)

    doc_type = fm.get("doc_type", "")
    feature_slug: str = fm.get("feature", "")

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
    await _write_nodes_and_edges(fm, path, project_slug, repo_root, headings, client, ctx, registry=registry)

    return True


# @spec SI-UNREG-001, SI-HEAD-002
async def ingest_doc_unregistered(
    path: Path,
    client: Any,
    project_slug: str,
    ctx: IngestionContext | None = None,
) -> None:
    """Ingest an unregistered doc as a bare Doc node with DocSection nodes but no Feature/Module/File edges."""
    if ctx is None:
        ctx = IngestionContext(project_slug=project_slug, repo_root=path.parent)

    commit_sha = get_commit_sha(path)
    doc_path_str = str(path)

    doc_node = Doc(
        node_type="Doc",
        project_slug=project_slug,
        doc_path=doc_path_str,
        doc_type="unregistered",
        feature_slug=None,
        commit_sha=commit_sha,
    )
    await client.upsert_node(doc_node)
    ctx.nodes_written += 1

    content = path.read_text(encoding="utf-8", errors="replace")
    headings = parse_headings(content)

    for heading_text, heading_slug, line_start, line_end in headings:
        section_node = DocSection(
            node_type="DocSection",
            project_slug=project_slug,
            doc_path=doc_path_str,
            heading_slug=heading_slug,
            heading_text=heading_text,
            doc_type="unregistered",
            line_start=line_start,
            line_end=line_end,
        )
        await client.upsert_node(section_node)
        ctx.nodes_written += 1
        # SI-HEAD-002: no DESCRIBED_BY edge for unregistered docs (no Feature node)


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



def _load_llm_config() -> dict:
    import tomllib
    config_path = Path.home() / ".modok" / "config.toml"
    if not config_path.exists():
        return {}
    with open(config_path, "rb") as f:
        data = tomllib.load(f)
    return data.get("llm", {})


def _is_interactive() -> bool:
    """Return False only when MODOK_NON_INTERACTIVE is set; otherwise assume interactive."""
    return not os.environ.get("MODOK_NON_INTERACTIVE", "")


@dataclass
class LLMProposalResult:
    valid_fields: dict[str, Any] = field(default_factory=dict)
    warnings: list[RejectedField] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    wrote_nothing: bool = False
    dry_run: bool = False
    suppressed: bool = False


# @spec SI-LLM-003, SI-LLM-004, SI-LLM-005, SI-LLM-006, SI-LLM-007,
#       SI-LLM-008, SI-LLM-010,
#       LLM-CEGIS-001, LLM-CEGIS-002, LLM-CEGIS-003, LLM-CEGIS-004, LLM-CEGIS-005
async def run_llm_proposal_pass(
    doc_path: Path,
    frontmatter: dict,
    missing_fields: list[str],
    registry: Registry,
    strict: bool = False,
    dry_run: bool = False,
    emit_counterexamples: bool = False,
) -> LLMProposalResult:
    """Run an LLM metadata proposal + CEGIS repair loop for one document."""
    # SI-LLM-008: non-interactive suppression
    if not _is_interactive():
        return LLMProposalResult(suppressed=True)

    cfg = _load_llm_config()

    # SI-LLM-007: validate counterexample config before any LLM call
    if emit_counterexamples:
        fixture_dir_str = cfg.get("counterexample_fixture_dir", "")
        if not fixture_dir_str:
            print("counterexample_fixture_dir is not configured in ~/.modok/config.toml [llm]",
                  file=sys.stderr)
            sys.exit(1)
        fixture_dir = Path(fixture_dir_str)

    cegis_enabled = bool(cfg.get("cegis_fix_enabled", False))

    # Initial proposal + verification
    proposal = await propose_metadata(
        doc_path=doc_path,
        frontmatter=frontmatter,
        missing_fields=missing_fields,
    )
    result_v = verify_proposal(proposal, missing_fields, frontmatter, registry)
    accumulated_valid: dict[str, Any] = dict(result_v.valid_fields)
    final_rejected: list[RejectedField] = list(result_v.rejected_fields)

    # CEGIS repair pass (at most one)
    if cegis_enabled and final_rejected:
        rejected_fields = [r.field for r in final_rejected]
        repair_context = [
            {
                "field": r.field,
                "bad_value": r.bad_value,
                "reason": r.reason,
                "repair_instruction": r.repair_instruction,
                **({"allowed_values": r.allowed_values} if r.allowed_values else {}),
            }
            for r in final_rejected
        ]
        repair_proposal = await propose_metadata(
            doc_path=doc_path,
            frontmatter=frontmatter,
            missing_fields=rejected_fields,
            repair_context=repair_context,
        )
        repair_v = verify_proposal(repair_proposal, rejected_fields, frontmatter, registry)
        accumulated_valid.update(repair_v.valid_fields)
        final_rejected = repair_v.rejected_fields

    # Emit counterexamples if requested and any fields still rejected
    if emit_counterexamples and final_rejected:
        data = {
            "case_id": str(uuid.uuid4()),
            "doc_path": str(doc_path),
            "counterexamples": [
                {
                    "field": r.field,
                    "bad_value": r.bad_value,
                    "reason": r.reason,
                    "repair_instruction": r.repair_instruction,
                }
                for r in final_rejected
            ],
        }
        fixture_path = fixture_dir / f"{data['case_id']}.yaml"
        fixture_path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")

    out = LLMProposalResult(dry_run=dry_run)

    if strict and final_rejected:
        out.wrote_nothing = True
        out.errors = [f"field '{r.field}' rejected: {r.reason}" for r in final_rejected]
        return out

    out.valid_fields = accumulated_valid
    out.warnings = final_rejected

    # SI-LLM-006: dry_run — do not write to disk
    if dry_run:
        return out

    # Write valid_fields into doc frontmatter and re-parse (stages 2-5)
    if accumulated_valid:
        if doc_path.exists():
            text = doc_path.read_text(encoding="utf-8")
            end = text.find("\n---", 3)
            if end != -1:
                fm_raw = text[3:end]
                try:
                    fm_data = yaml.safe_load(fm_raw) or {}
                except Exception:
                    fm_data = {}
                modok_block = fm_data.get("modok", {}) or {}
                modok_block.update(accumulated_valid)
                fm_data["modok"] = modok_block
                new_fm = yaml.dump(fm_data, default_flow_style=False)
                new_content = f"---\n{new_fm}---{text[end + 4:]}"
                doc_path.write_text(new_content, encoding="utf-8")

        # SI-LLM-010: re-run stages 2-5 (parse, validate, check refs) — not discovery or sha
        import modok.ingestion.parser as _parser
        _parser.parse_frontmatter(doc_path)

    return out


async def run_ingestion(
    repo_root: Path,
    registry: Registry,
    client: Any,
    project_slug: str,
) -> IngestionReport:
    """Top-level entry point. Discover, parse, ingest all docs under repo_root."""
    import time
    report = IngestionReport()
    ctx = IngestionContext(project_slug=project_slug, repo_root=repo_root)

    report.warnings.extend(check_working_tree(repo_root))

    t0 = time.monotonic()
    from modok.ingestion.discovery import discover_docs
    registered, unregistered, ignored_count = discover_docs(repo_root, registry)
    report.files_ignored = ignored_count

    for record in registered:
        try:
            await ingest_doc(
                record.path,
                registry=registry,
                client=client,
                project_slug=project_slug,
                repo_root=repo_root,
                ctx=ctx,
                doc_record=record,
            )
            report.docs_processed += 1
        except InvalidSlugReferenceError as exc:
            report.errors.append(str(exc))
        except Exception as exc:
            report.errors.append(f"{record.path}: {exc}")

    for record in unregistered:
        try:
            await ingest_doc_unregistered(record.path, client=client, project_slug=project_slug, ctx=ctx)
            report.unregistered_count += 1
            report.unregistered_paths.append(str(record.path))
        except Exception as exc:
            report.errors.append(f"{record.path}: {exc}")

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
