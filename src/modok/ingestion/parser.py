from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ParsedDoc:
    path: Path
    frontmatter: dict
    modok_blocks: list[dict]
    headings: list[tuple[str, str, int, int | None]]  # (text, slug, line_start, line_end)
    commit_sha: str | None = None


@dataclass
class ParseResult:
    docs: list[ParsedDoc] = field(default_factory=list)
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def parse_frontmatter(path: Path) -> dict | None:
    """Return parsed modok: block or None if absent."""
    raise NotImplementedError


def parse_modok_blocks(content: str) -> list[dict]:
    """Extract fenced modok blocks from doc body."""
    raise NotImplementedError


def parse_headings(content: str) -> list[tuple[str, str, int, int | None]]:
    """Extract H2/H3 headings with line ranges."""
    raise NotImplementedError


def get_commit_sha(path: Path) -> str | None:
    """Run git log -1 to get most recent SHA for file."""
    raise NotImplementedError


def is_working_tree_dirty(repo_root: Path) -> bool:
    raise NotImplementedError
