from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from modok.ingestion.registry import Registry

IGNORE_DIR_NAMES = frozenset(
    [
        ".git",
        "node_modules",
        "bin",
        "obj",
        "dist",
        "build",
        "coverage",
        ".vs",
    ]
)

IGNORE_SUFFIXES = frozenset([".key", ".pem", ".pfx"])
IGNORE_NAMES = frozenset([".env"])
SUPPORTED_SUFFIXES = frozenset([".md", ".mdx", ".yaml", ".yml"])


def _is_ignored(path: Path) -> bool:
    for part in path.parts:
        if part in IGNORE_DIR_NAMES:
            return True
    if path.suffix in IGNORE_SUFFIXES:
        return True
    if path.name in IGNORE_NAMES:
        return True
    return False


def discover_files(root: Path) -> tuple[list[Path], int]:
    """Return (supported_files, ignored_count).

    Supported files have a supported extension and are not in an ignored path.
    Frontmatter filtering (skipped_count) is handled by the pipeline stage.
    """
    found: list[Path] = []
    ignored = 0

    for candidate in root.rglob("*"):
        if not candidate.is_file():
            continue
        rel = candidate.relative_to(root)
        if _is_ignored(rel):
            ignored += 1
            continue
        if candidate.suffix not in SUPPORTED_SUFFIXES:
            ignored += 1
            continue
        found.append(candidate)

    return found, ignored


@dataclass
class DocRecord:
    path: Path
    doc_type: str
    feature: str | None
    tier: int  # 1 = arrow index, 2 = path inference, 3 = unregistered
    modules: list[str] = field(default_factory=list)
    source_files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    is_unregistered: bool = False
    warned: bool = False  # True when implicit inference failed (vs explicit declaration)


# @spec SI-DISC-003, SI-TIER1-001, SI-TIER1-002, SI-TIER2-001, SI-TIER2-002, SI-TIER2-003,
#       SI-UNREG-001, SI-UNREG-002, SI-FMTR-001, SI-FMTR-002
def discover_docs(
    repo_root: Path,
    registry: "Registry",
) -> tuple[list[DocRecord], list[DocRecord], int]:
    """Three-tier doc discovery.

    Returns (registered_docs, unregistered_docs, files_ignored).
    Raises InvalidSlugReferenceError when a feature slug is explicitly declared
    in frontmatter but not present in the registry (SI-FMTR-002 / SI-REF-001).
    """
    from modok.ingestion.errors import InvalidSlugReferenceError

    arrow_index_path = repo_root / "docs" / "arrows" / "index.yaml"
    arrow_entries: list[dict] = []
    if arrow_index_path.exists():
        raw = yaml.safe_load(arrow_index_path.read_text(encoding="utf-8")) or {}
        arrow_entries = raw.get("arrows", []) or []

    # --- Tier 1: Arrow-index-driven ---
    tier1_paths: dict[Path, DocRecord] = {}
    for entry in arrow_entries:
        feature_id = entry.get("id", "")
        src_files = registry.source_files_for_feature(feature_id)
        tst_files = registry.test_files_for_feature(feature_id)

        mod_slugs = registry.modules_for_feature(feature_id)
        for field_name, doc_type in [("arrow_doc", "hld"), ("lld", "lld"), ("specs", "spec")]:
            field_value = entry.get(field_name)
            if not field_value:
                continue
            # @spec SI-TIER1-003 — arrow_doc/lld/specs may be a single path
            # or a list of paths (a feature spanning more than one LLD, e.g.).
            rel_paths = field_value if isinstance(field_value, list) else [field_value]
            for rel_path in rel_paths:
                abs_path = repo_root / rel_path
                rec = DocRecord(
                    path=abs_path,
                    doc_type=doc_type,
                    feature=feature_id,
                    tier=1,
                    modules=list(mod_slugs),
                    source_files=list(src_files),
                    test_files=list(tst_files),
                )
                tier1_paths[abs_path] = rec

    # --- Scan docs/ for all .md files ---
    docs_root = repo_root / "docs"
    all_doc_files: list[Path] = []
    ignored_count = 0
    if docs_root.exists():
        for candidate in docs_root.rglob("*.md"):
            if not candidate.is_file():
                continue
            rel = candidate.relative_to(repo_root)
            if _is_ignored(rel):
                ignored_count += 1
                continue
            all_doc_files.append(candidate)

    registered: list[DocRecord] = list(tier1_paths.values())
    unregistered: list[DocRecord] = []

    for path in all_doc_files:
        # Normalise path for comparison
        norm = path if path.is_absolute() else repo_root / path
        if norm in tier1_paths or path in tier1_paths:
            continue  # already claimed by Tier 1

        fm = _read_modok_frontmatter(path)

        # Explicit unregistered declaration — no warning
        if fm.get("doc_type") == "unregistered":
            unregistered.append(
                DocRecord(
                    path=path,
                    doc_type="unregistered",
                    feature=None,
                    tier=3,
                    is_unregistered=True,
                    warned=False,
                )
            )
            continue

        # Tier 2: path + stem inference
        inferred_doc_type = _infer_doc_type(path, repo_root)
        inferred_feature = _infer_feature(path, inferred_doc_type)

        # Apply frontmatter overrides
        fm_feature = fm.get("feature")
        fm_doc_type = fm.get("doc_type")

        final_doc_type = fm_doc_type or inferred_doc_type

        if fm_feature:
            # Explicit frontmatter slug — must be in registry (SI-REF-001 / SI-FMTR-002)
            if not registry.has_feature(fm_feature):
                raise InvalidSlugReferenceError(
                    f"feature slug {fm_feature!r} declared in frontmatter is not in the feature registry"
                )
            final_feature: str | None = fm_feature
        else:
            final_feature = inferred_feature

        if final_feature and registry.has_feature(final_feature):
            registered.append(
                DocRecord(
                    path=path,
                    doc_type=final_doc_type or "hld",
                    feature=final_feature,
                    tier=2,
                    modules=list(registry.modules_for_feature(final_feature)),
                    source_files=list(registry.source_files_for_feature(final_feature)),
                    test_files=list(registry.test_files_for_feature(final_feature)),
                )
            )
        else:
            # Tier 3: inferred slug not in registry → unregistered with warning
            unregistered.append(
                DocRecord(
                    path=path,
                    doc_type="unregistered",
                    feature=None,
                    tier=3,
                    is_unregistered=True,
                    warned=True,
                )
            )

    return registered, unregistered, ignored_count


def _infer_doc_type(path: Path, repo_root: Path) -> str:
    try:
        rel = path.relative_to(repo_root / "docs")
    except ValueError:
        return "hld"

    parts = rel.parts
    if len(parts) >= 2:
        subdir = parts[0]
        if subdir == "llds":
            return "lld"
        elif subdir == "arrows":
            return "hld"
        elif subdir == "specs":
            return "spec"
    return "hld"


def _infer_feature(path: Path, doc_type: str) -> str:
    stem = path.stem
    if doc_type == "spec" and stem.endswith("-specs"):
        stem = stem[:-6]
    return stem


def _read_modok_frontmatter(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block_text = text[3:end]
    if "modok:" not in block_text:
        return {}
    try:
        data = yaml.safe_load(block_text) or {}
        return data.get("modok", {}) or {}
    except Exception:
        return {}


def has_modok_frontmatter(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if not text.startswith("---"):
        return False
    # Find closing ---
    end = text.find("\n---", 3)
    if end == -1:
        return False
    block = text[3:end]
    return "modok:" in block
