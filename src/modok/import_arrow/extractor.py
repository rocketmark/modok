# @spec IA-FEAT-001, IA-FEAT-002, IA-FEAT-003, IA-FEAT-004, IA-FEAT-005, IA-FEAT-006, IA-FEAT-007, IA-FEAT-008, IA-FEAT-009, IA-UNCLAIMED-001
"""Feature extraction from arrow index and arrow docs."""

from __future__ import annotations

import re
import sys
from pathlib import Path


def slug_to_name(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.split("-"))


def extract_source_files_from_doc(doc_content: str) -> list[str] | None:
    """Extract source file paths from the ### Code section.

    Returns None when the section is absent (caller should warn).
    """
    section = _extract_section(doc_content, "Code")
    if section is None:
        return None

    paths = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        # Split on em-dash variants; take only left side.
        left = re.split(r"\s*[—–]|\s+-\s+", line, maxsplit=1)[0]
        for item in re.findall(r"`([^`]+)`", left):
            if ("/" in item or "." in item) and "(" not in item:
                paths.append(item)
    return paths


def parse_key_components_from_doc(doc_content: str) -> list[dict]:
    """Extract Key Components entries from an arrow doc."""
    from modok.import_arrow.modules import parse_key_components

    section = _extract_section(doc_content, "Key Components")
    if section is None:
        return []
    return parse_key_components(section)


def validate_source_files(
    slug: str,
    source_files: list[str],
    code_map: dict,
    warn,
) -> None:
    """Validate source files against the code map; call warn(msg) for each issue."""
    index = {e["path"]: e for e in code_map.get("files", [])}
    for path in source_files:
        if path not in index:
            warn(f"WARN [{slug}]: source file not in code map: {path}")
        elif index[path].get("role") == "ignored":
            warn(f"WARN [{slug}]: source file is ignored in code map: {path}")


def arrow_doc_paths(entry: dict) -> list[str]:
    """Normalize an arrow entry's arrow_doc field to a list of paths.

    arrow_doc may be a single string or a list (a feature spanning more
    than one arrow doc, e.g. a client-side and a Pi-side half).
    """
    value = entry.get("arrow_doc")
    if not value:
        return []
    return list(value) if isinstance(value, list) else [value]


def extract_features(index_data: list[dict], repo_root: Path) -> dict:
    """Extract features dict keyed by slug from arrow index entries."""
    features = {}
    for entry in index_data:
        slug = entry["id"]

        # @spec IA-FEAT-011
        source_files: list[str] = []
        for doc_rel_path in arrow_doc_paths(entry):
            doc_content = (repo_root / doc_rel_path).read_text(encoding="utf-8")
            result = extract_source_files_from_doc(doc_content)
            if result is None:
                print(
                    f"WARN [{slug}]: arrow doc has no ### Code section — {doc_rel_path}",
                    file=sys.stderr,
                )
                continue
            for p in result:
                if p not in source_files:
                    source_files.append(p)

        features[slug] = {
            "name": slug_to_name(slug),
            "description": entry["description"],
            "source_files": source_files,
            "test_files": _clean_test_files(entry.get("tests", [])),
            "specs": entry["specs"],
            "modules": [],
        }
    return features


def report_unclaimed_files(features: dict, code_map: dict) -> None:
    """Print source files in the code map not claimed by any feature."""
    claimed = set()
    for feat in features.values():
        claimed.update(feat.get("source_files", []))

    unclaimed = sorted(
        e["path"]
        for e in code_map.get("files", [])
        if e.get("role") == "source" and e["path"] not in claimed
    )

    print(f"{len(unclaimed)} source files in code map not claimed by any arrow:")
    for path in unclaimed:
        print(f"  {path}")


def _extract_section(doc_content: str, section_name: str) -> str | None:
    # Match either H3 header (### Key Components) or bold label (**Key Components:**)
    escaped = re.escape(section_name)
    pattern = rf"(?:###\s+{escaped}|[*][*]{escaped}[:\s]*[*][*])[^\n]*\n(.*?)(?=\n##|\Z)"
    m = re.search(pattern, doc_content, re.DOTALL)
    return m.group(1) if m else None


def _clean_test_files(tests: list) -> list[str]:
    result = []
    for t in tests:
        # @spec IA-FEAT-010 — an unquoted "tests:" list item containing a
        # colon (e.g. "foo.c (label: detail)") parses as a single-key YAML
        # mapping rather than a scalar string; skip rather than crash.
        if not isinstance(t, str):
            print(f"WARN: skipping malformed tests entry (not a string): {t!r}", file=sys.stderr)
            continue
        if t.startswith("spec/"):
            continue
        t = t.split("(")[0].strip()
        if "/" not in t and "." not in t:
            continue
        result.append(t)
    return result
