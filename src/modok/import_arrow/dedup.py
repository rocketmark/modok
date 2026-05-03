# @spec IA-DEDUP-001, IA-DEDUP-002, IA-DEDUP-003, IA-DEDUP-004, IA-LLM-006
"""Deduplication pass for module candidates."""
from __future__ import annotations

import sys
from collections import defaultdict
from itertools import combinations


def build_inverted_index(modules: dict) -> dict[str, list[str]]:
    """Build source_file → [module slugs] inverted index."""
    index: dict[str, list[str]] = defaultdict(list)
    for slug, entry in modules.items():
        for f in entry.get("source_files", []):
            index[f].append(slug)
    return dict(index)


def find_duplicate_pairs(modules: dict) -> list[tuple[str, str]]:
    """Return all slug pairs whose source_files frozensets are identical."""
    file_sets = {
        slug: frozenset(entry.get("source_files", []))
        for slug, entry in modules.items()
    }
    return [
        (a, b)
        for a, b in combinations(file_sets.keys(), 2)
        if file_sets[a] == file_sets[b]
    ]


def check_subset_pairs(modules: dict) -> None:
    """Warn on strict subset relationships between non-empty module file sets."""
    file_sets = {
        slug: frozenset(entry.get("source_files", []))
        for slug, entry in modules.items()
        if entry.get("source_files")  # skip directory-level modules (source_files: [])
    }
    for a, b in combinations(file_sets.keys(), 2):
        if file_sets[a] < file_sets[b]:
            print(
                f"WARN: [{a}] source_files is a strict subset of [{b}] — may be redundant",
                file=sys.stderr,
            )
        elif file_sets[b] < file_sets[a]:
            print(
                f"WARN: [{b}] source_files is a strict subset of [{a}] — may be redundant",
                file=sys.stderr,
            )


def check_shared_files(modules: dict) -> None:
    """Warn when a source file is claimed by multiple non-duplicate modules."""
    index = build_inverted_index(modules)
    file_sets = {
        slug: frozenset(entry.get("source_files", []))
        for slug, entry in modules.items()
    }
    warned: set[str] = set()
    for file_path, slugs in index.items():
        if len(slugs) <= 1:
            continue
        for a, b in combinations(slugs, 2):
            if file_sets.get(a) != file_sets.get(b) and file_path not in warned:
                print(
                    f"WARN: {file_path} claimed by multiple modules: {sorted(slugs)}",
                    file=sys.stderr,
                )
                warned.add(file_path)
                break


def resolve_duplicates_no_llm(pairs: list[tuple[str, str]], modules: dict) -> dict:
    """With --no-llm: warn about duplicate pairs and retain both entries."""
    for a, b in pairs:
        print(
            f"WARN: duplicate modules [{a}] and [{b}] share the same source files. "
            f"Run without --no-llm to resolve. Retaining both.",
            file=sys.stderr,
        )
    return modules
