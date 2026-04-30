from __future__ import annotations
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml


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
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    if not text.startswith("---"):
        return None

    end = text.find("\n---", 3)
    if end == -1:
        return None

    raw = text[3:end]
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None

    if not isinstance(data, dict):
        return None

    modok_block = data.get("modok")
    if not isinstance(modok_block, dict):
        return None

    return modok_block


_MODOK_FENCE_RE = re.compile(
    r"^```modok\s*\n(.*?)^```",
    re.MULTILINE | re.DOTALL,
)


def parse_modok_blocks(content: str) -> list[dict]:
    """Extract fenced modok blocks from doc body."""
    blocks = []
    for m in _MODOK_FENCE_RE.finditer(content):
        raw = m.group(1)
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError:
            continue
        if isinstance(data, dict):
            blocks.append(data)
    return blocks


_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+)$", re.MULTILINE)


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def parse_headings(content: str) -> list[tuple[str, str, int, int | None]]:
    """Extract H2/H3 headings with line ranges."""
    lines = content.splitlines()
    results: list[tuple[str, str, int, int | None]] = []

    for i, line in enumerate(lines, start=1):
        m = re.match(r"^(#{2,3})\s+(.+)$", line)
        if m:
            text = m.group(2).strip()
            slug = _slugify(text)
            results.append((text, slug, i, None))

    # Fill in line_end for each heading
    out = []
    for idx, (text, slug, line_start, _) in enumerate(results):
        if idx + 1 < len(results):
            line_end = results[idx + 1][2] - 1
        else:
            line_end = None
        out.append((text, slug, line_start, line_end))

    return out


def get_commit_sha(path: Path) -> str | None:
    """Run git log -1 to get most recent SHA for file."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", str(path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        sha = result.stdout.strip()
        return sha if sha else None
    except OSError:
        return None


def is_working_tree_dirty(repo_root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        return bool(result.stdout.strip())
    except OSError:
        return False
