# @spec RP-PARSE-001, RP-PARSE-002, RP-PARSE-003, RP-PARSE-004, RP-PARSE-005, RP-PARSE-006
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Section:
    heading: str
    body: str
    doc_path: Path


def parse_sections(path: Path) -> list[Section]:
    """Parse a doc file into H2-delimited Section objects.

    Pure function — reads the file and returns sections with no LLM calls or side effects.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    text = _strip_frontmatter(text)
    return _split_on_h2(text, path)


def _strip_frontmatter(text: str) -> str:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "".join(lines[i + 1 :])
    return text  # no closing --- found; return as-is


def _heading_level(line: str) -> int:
    stripped = line.rstrip("\n").rstrip()
    count = 0
    for c in stripped:
        if c == "#":
            count += 1
        else:
            break
    if count == 0:
        return 0
    # Must be followed by a space (or be just hashes) to be a heading
    if len(stripped) > count and stripped[count] != " ":
        return 0
    return count


def _split_on_h2(text: str, doc_path: Path) -> list[Section]:
    lines = text.splitlines(keepends=True)
    sections: list[Section] = []
    current_heading: str | None = None
    current_body_lines: list[str] = []

    def _flush() -> None:
        if current_heading is None:
            return
        body = "".join(current_body_lines).strip()
        if body:
            sections.append(Section(heading=current_heading, body=body, doc_path=doc_path))

    for line in lines:
        level = _heading_level(line)
        if level == 1:
            # H1 — skip entirely (not a section boundary, not part of body)
            continue
        elif level == 2:
            _flush()
            current_heading = line.lstrip("#").strip()
            current_body_lines = []
        else:
            if current_heading is not None:
                current_body_lines.append(line)

    _flush()
    return sections
