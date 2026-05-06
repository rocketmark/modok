# @spec IA-MOD-001, IA-MOD-002, IA-MOD-003, IA-MOD-003b, IA-MOD-004, IA-MOD-005, IA-OVER-001, IA-OVER-002, IA-OVER-003, IA-OVER-004, IA-OVER-005, IA-OVER-006
"""Module extraction passes 1 (mechanical) and 2 (Key Components overlay)."""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path


from modok.import_arrow.extractor import slug_to_name as _title

_NON_SOURCE_ROLES = {"config", "docs", "generated", "ignored"}


def _slug_from_stem(stem: str) -> str:
    return stem.replace("_", "-")


def _slug_from_dir(dir_path: str) -> str:
    return dir_path.replace("/", "-").replace("_", "-").strip("-")


def build_modules_pass1(features: dict, code_map: dict) -> dict:
    """Pass 1: file-per-module for Python; directory-level for C/cpp groups."""
    cm_index = {e["path"]: e for e in code_map.get("files", [])}

    # Collect unique source files across all features
    seen: set[str] = set()
    all_paths: list[str] = []
    for feat in features.values():
        for path in feat.get("source_files", []):
            if path not in seen:
                seen.add(path)
                all_paths.append(path)

    # Group cpp files by parent directory
    dir_cpp: dict[str, list[str]] = defaultdict(list)
    for path in all_paths:
        entry = cm_index.get(path, {})
        if entry.get("language") == "cpp" and entry.get("role", "source") not in _NON_SOURCE_ROLES:
            parent = str(Path(path).parent)
            dir_cpp[parent].append(path)

    # Directories with ≥ 2 cpp files become directory-level modules
    cpp_dirs: set[str] = {d for d, files in dir_cpp.items() if len(files) >= 2}

    # Detect stem collisions for disambiguated slugs
    stem_paths: dict[str, list[str]] = defaultdict(list)
    for path in all_paths:
        entry = cm_index.get(path, {})
        role = entry.get("role", "source")
        lang = entry.get("language", "unknown")
        if role in _NON_SOURCE_ROLES:
            continue
        if lang == "cpp" and str(Path(path).parent) in cpp_dirs:
            continue
        stem_paths[Path(path).stem].append(path)

    ambiguous_stems = {stem for stem, paths in stem_paths.items() if len(paths) > 1}

    modules: dict[str, dict] = {}

    # Directory-level modules for cpp groups
    for dir_path in sorted(cpp_dirs):
        slug = _slug_from_dir(dir_path)
        modules[slug] = {
            "name": _title(slug),
            "description": "",
            "source_files": [],
            "source_roots": [dir_path],
            "primary_class": "",
            "language": "cpp",
        }

    # File-level modules for everything else
    for path in all_paths:
        entry = cm_index.get(path, {})
        role = entry.get("role", "source")
        lang = entry.get("language", "unknown")

        if role in _NON_SOURCE_ROLES:
            continue
        if lang == "cpp" and str(Path(path).parent) in cpp_dirs:
            continue

        if path not in cm_index:
            print(f"WARN: source file not in code map, using defaults: {path}", file=sys.stderr)
            lang = "unknown"
            symbols = []
        else:
            symbols = entry.get("symbols", [])

        stem = Path(path).stem
        if stem in ambiguous_stems:
            parent_name = Path(path).parent.name.replace("_", "-")
            slug = f"{parent_name}-{_slug_from_stem(stem)}"
        else:
            slug = _slug_from_stem(stem)

        primary_class = symbols[0]["name"] if symbols else ""

        modules[slug] = {
            "name": _title(slug),
            "description": "",
            "source_files": [path],
            "source_roots": [],
            "primary_class": primary_class,
            "language": lang,
        }

    return modules


def parse_key_components(section: str) -> list[dict]:
    """Parse numbered list items from a Key Components section body."""
    components = []
    for line in section.splitlines():
        line = line.strip()
        m = re.match(r'\d+\.\s+`([^`]+)`\s*(?:—|–|-)\s*(.+)', line)
        if m:
            components.append({
                "class_name": m.group(1).strip(),
                "description": m.group(2).strip(),
            })
    return components


def apply_key_components_overlay(
    modules: dict,
    arrow_key_components: dict[str, list[dict]],
    code_map: dict,
) -> dict:
    """Pass 2: apply Key Components descriptions to module entries."""
    # Symbol → [file paths] from code map
    sym_to_files: dict[str, list[str]] = defaultdict(list)
    for entry in code_map.get("files", []):
        for sym in entry.get("symbols", []):
            sym_to_files[sym["name"]].append(entry["path"])

    # File path → module slug
    file_to_slug: dict[str, str] = {}
    for slug, entry in modules.items():
        for f in entry.get("source_files", []):
            file_to_slug[f] = slug

    # Class name → [arrow ids] — detect cross-arrow ambiguity
    class_to_arrows: dict[str, list[str]] = defaultdict(list)
    for arrow_id, components in arrow_key_components.items():
        for comp in components:
            class_to_arrows[comp["class_name"]].append(arrow_id)

    for arrow_id, components in arrow_key_components.items():
        for comp in components:
            class_name = comp["class_name"]
            description = comp["description"]

            if len(class_to_arrows[class_name]) > 1:
                print(
                    f"WARN: ambiguous Key Components symbol '{class_name}' "
                    f"— appears in multiple arrows, skipping overlay",
                    file=sys.stderr,
                )
                continue

            # Strip trailing () — arrow docs often use `func()` notation
            lookup_name = class_name.rstrip("()")
            files = sym_to_files.get(lookup_name, [])
            if not files:
                print(
                    f"WARN: symbol '{lookup_name}' not in code map symbol table, "
                    f"skipping Key Components match",
                    file=sys.stderr,
                )
                continue
            if len(files) > 1:
                print(
                    f"WARN: ambiguous symbol '{class_name}' found in multiple files "
                    f"{files}, skipping Key Components match",
                    file=sys.stderr,
                )
                continue

            slug = file_to_slug.get(files[0])
            if slug and slug in modules:
                modules[slug]["description"] = description

    return modules
