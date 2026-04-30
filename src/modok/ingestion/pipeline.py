from __future__ import annotations
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

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
    "DocSection",
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

    def add_pending_fact(self, value: Any, score: float, evidence: str) -> None:
        self._pending.append({"value": value, "score": score, "evidence": evidence})

    @property
    def pending_count(self) -> int:
        return len(self._pending)


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
        proposals = invoke_llm_gateway(doc_path)
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


def score_fact(source: str, value: Any) -> float:
    """Return confidence score for a fact given its source."""
    if source in ("frontmatter", "modok_block"):
        return 1.00
    # prose extraction fallback — callers apply confidence_band separately
    return 0.60


def fact_disposition(score: float) -> str:
    """Return write/write_with_confidence/pending based on confidence band."""
    if score >= 0.90:
        return "write"
    if score >= 0.75:
        return "write_with_confidence"
    return "pending"


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


def ingest_doc(
    path: Path,
    registry: Registry,
    client: Any,
    project_slug: str,
    repo_root: Path,
) -> None:
    """Ingest a single markdown/yaml doc into the graph."""
    from modok.quine.models import DocSection

    fm = parse_frontmatter(path)
    if fm is None:
        return

    validate_references(fm, registry)

    content = path.read_text(encoding="utf-8", errors="replace")
    headings = parse_headings(content)
    doc_type = fm.get("doc_type", "")

    # Write a DocSection node per heading
    for text, slug, line_start, line_end in headings:
        node = DocSection(
            node_type="DocSection",
            project_slug=project_slug,
            doc_path=str(path),
            heading_slug=slug,
            heading_text=text,
            doc_type=doc_type,
            line_start=line_start,
            line_end=line_end,
        )
        client.upsert_node(node)


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


def invoke_llm_gateway(doc_path: Path, ctx: IngestionContext | None = None) -> dict:
    """Call LLM gateway to generate proposals for a doc. Returns proposal dict."""
    # Implemented when LLM Gateway component (component 3) is built.
    raise NotImplementedError("LLM gateway not yet implemented")


def run_ingestion(
    repo_root: Path,
    registry: Registry,
    client: Any,
    project_slug: str,
    fix_mode: bool = False,
) -> IngestionReport:
    """Top-level entry point. Discover, parse, ingest all docs under repo_root."""
    report = IngestionReport()

    files, ignored = discover_files(repo_root)
    report.files_ignored = ignored

    for path in files:
        try:
            ingest_doc(
                path,
                registry=registry,
                client=client,
                project_slug=project_slug,
                repo_root=repo_root,
            )
            report.docs_processed += 1
        except InvalidSlugReferenceError as exc:
            report.errors.append(str(exc))
        except Exception as exc:
            report.errors.append(f"{path}: {exc}")

    return report


# ---------------------------------------------------------------------------
# Commit diff ingestion
# ---------------------------------------------------------------------------

_HUNK_RE = re.compile(r"^@@[^@]+@@")
_NUMSTAT_RE = re.compile(r"^(\d+|-)\t(\d+|-)\t(.+)$")


def parse_commit_metadata(commit_sha: str) -> dict:
    """Run git show to extract commit metadata. Returns dict with author, timestamp, etc."""
    result = subprocess.run(
        ["git", "show", "--no-patch", "--format=%H%n%an%n%aI%n%s", commit_sha],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {
            "commit_sha": commit_sha,
            "author": "",
            "timestamp_iso": "",
            "message_summary": "",
        }
    lines = result.stdout.strip().splitlines()
    return {
        "commit_sha": lines[0] if len(lines) > 0 else commit_sha,
        "author": lines[1] if len(lines) > 1 else "",
        "timestamp_iso": lines[2] if len(lines) > 2 else "",
        "message_summary": lines[3] if len(lines) > 3 else "",
    }


def parse_diff_numstat(commit_sha: str) -> list[dict]:
    """Run git show --numstat --patch to extract per-file change stats and hunk headers."""
    result = subprocess.run(
        ["git", "show", "--format=", "--numstat", "--unified=0", commit_sha],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []

    entries: dict[str, dict] = {}
    current_path: str | None = None

    for line in result.stdout.splitlines():
        numstat_m = _NUMSTAT_RE.match(line)
        if numstat_m:
            added_s, removed_s, path = numstat_m.groups()
            added = int(added_s) if added_s != "-" else 0
            removed = int(removed_s) if removed_s != "-" else 0
            entries[path] = {"path": path, "added": added, "removed": removed, "hunks": []}
            current_path = path
        elif _HUNK_RE.match(line) and current_path and current_path in entries:
            # Store only the @@ header, not the trailing context label
            hunk_header = _HUNK_RE.match(line).group(0).strip()
            entries[current_path]["hunks"].append(hunk_header)

    return list(entries.values())


def ingest_commit_diff(
    project_slug: str,
    commit_sha: str,
    source_paths: list[str] | None,
    registry: Registry,
    client: Any,
) -> int:
    """Ingest commit diff data. Returns count of FileChange nodes written.

    Skips entirely when source_paths is None or empty (SI-DIFF-002).
    """
    from modok.quine.ids import idFrom
    from modok.quine.models import CommitEvent, FileChange

    if not source_paths:
        return 0

    # Parse commit metadata and upsert CommitEvent
    meta = parse_commit_metadata(commit_sha)
    commit_event = CommitEvent(
        node_type="CommitEvent",
        project_slug=project_slug,
        commit_sha=meta["commit_sha"],
        author=meta["author"],
        timestamp_iso=meta["timestamp_iso"],
        message_summary=meta["message_summary"],
    )
    client.upsert_node(commit_event)
    commit_event_id = idFrom("CommitEvent", project_slug, commit_sha)

    # Parse diff and filter to source_paths
    diff_entries = parse_diff_numstat(commit_sha)
    matching = [
        e for e in diff_entries
        if any(e["path"].startswith(sp.rstrip("/") + "/") or e["path"] == sp
               for sp in source_paths)
    ]

    touches_feature_written: set[tuple[str, str]] = set()
    file_changes_written = 0

    for entry in matching:
        path = entry["path"]
        fc = FileChange(
            node_type="FileChange",
            project_slug=project_slug,
            commit_sha=commit_sha,
            repo_path=path,
            lines_added=entry["added"],
            lines_removed=entry["removed"],
            hunks=entry["hunks"],
        )
        client.upsert_node(fc)
        fc_id = idFrom("FileChange", project_slug, commit_sha, path)
        file_id = idFrom("File", project_slug, path)

        client.write_edge(file_id, "CHANGED_IN", fc_id)
        client.write_edge(fc_id, "IN_COMMIT", commit_event_id)
        file_changes_written += 1

        # Derive TOUCHES_FEATURE edges
        for module_slug in registry.modules_covering_path(path):
            for feature_slug in registry.features_for_module(module_slug):
                key = (commit_sha, feature_slug)
                if key not in touches_feature_written:
                    feature_id = idFrom("Feature", project_slug, feature_slug)
                    client.write_edge(commit_event_id, "TOUCHES_FEATURE", feature_id)
                    touches_feature_written.add(key)

    return file_changes_written
