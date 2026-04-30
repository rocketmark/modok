from __future__ import annotations
from pathlib import Path

IGNORE_PATTERNS = [
    ".git",
    "node_modules",
    "bin",
    "obj",
    "dist",
    "build",
    "coverage",
    ".vs",
]

IGNORE_SUFFIXES = [".key", ".pem", ".pfx"]
IGNORE_NAMES = [".env"]
SUPPORTED_SUFFIXES = {".md", ".mdx", ".yaml", ".yml"}


def _is_ignored(path: Path) -> bool:
    for part in path.parts:
        if part in IGNORE_PATTERNS:
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
