# @spec IA-LLM-001, IA-LLM-002, IA-LLM-003, IA-LLM-004, IA-LLM-005, IA-LLM-007, IA-LLM-008
"""LLM passes for name/description generation and dedup resolution."""
from __future__ import annotations

import sys
from typing import Callable


def filter_modules_for_llm_pass(modules: dict) -> list[dict]:
    """Return modules that need LLM name/description generation.

    Skip when: description is non-empty AND slug is self-evidently title-caseable.
    """
    candidates = []
    for slug, entry in modules.items():
        description = entry.get("description", "")
        if description and _is_self_evident_slug(slug):
            continue
        candidates.append({
            "slug": slug,
            "primary_class": entry.get("primary_class", ""),
            "source_file": (entry.get("source_files") or [""])[0],
            "language": entry.get("language", "unknown"),
        })
    return candidates


def _is_self_evident_slug(slug: str) -> bool:
    """Return True if every slug component is a common recognisable word.

    Acronym-like components (short, non-dictionary) make a slug non-self-evident.
    This is a heuristic; it intentionally errs on the side of calling things
    self-evident rather than over-batching to the LLM.
    """
    _known_acronyms = {
        "shtp", "ltc", "smll", "usb", "udp", "tcp", "ip", "llm", "api",
        "rpc", "ssl", "tls", "xml", "json", "csv", "sql", "url", "uri",
    }
    for part in slug.split("-"):
        if part in _known_acronyms:
            return False
    return True


def run_name_description_pass(
    modules_to_name: list[dict],
    llm_call: Callable,
) -> dict[str, dict]:
    """Batched LLM call for name/description generation.

    On failure, falls back to title-cased slug defaults and warns.
    """
    try:
        results = llm_call(modules_to_name)
        return {r["slug"]: {"name": r["name"], "description": r["description"]} for r in results}
    except Exception as exc:
        print(
            f"WARN: LLM name/description pass failed ({exc}) — "
            f"falling back to title-cased slug defaults for all affected modules",
            file=sys.stderr,
        )
        return {
            m["slug"]: {
                "name": " ".join(w.capitalize() for w in m["slug"].split("-")),
                "description": "",
            }
            for m in modules_to_name
        }


def run_dedup_pass(
    pairs: list[tuple[str, str]],
    modules: dict,
    llm_call: Callable,
) -> tuple[dict, list[str]]:
    """One LLM call per duplicate pair for concept-identity determination.

    Returns (resolved_modules, conflict_messages).
    On LLM failure for a pair, treats as CONFLICT.
    """
    result_modules = dict(modules)
    conflicts: list[str] = []

    for slug_a, slug_b in pairs:
        entry_a = modules.get(slug_a, {})
        entry_b = modules.get(slug_b, {})
        shared = list(
            frozenset(entry_a.get("source_files", [])) & frozenset(entry_b.get("source_files", []))
        )
        payload = {
            "slug_a": slug_a,
            "name_a": entry_a.get("name", ""),
            "description_a": entry_a.get("description", ""),
            "slug_b": slug_b,
            "name_b": entry_b.get("name", ""),
            "description_b": entry_b.get("description", ""),
            "shared_files": shared,
        }

        try:
            response = llm_call(payload)
        except Exception as exc:
            conflicts.append(
                f"{slug_a} vs {slug_b} — LLM dedup failed ({exc}). Human review required."
            )
            continue

        if not response.get("same_concept", True):
            conflicts.append(
                f"{slug_a} vs {slug_b} — same files, different concepts. Human review required."
            )
        else:
            keep = response.get("keep", slug_a)
            discard = slug_b if keep == slug_a else slug_a
            if response.get("description"):
                result_modules[keep] = dict(result_modules[keep])
                result_modules[keep]["description"] = response["description"]
            result_modules.pop(discard, None)

    return result_modules, conflicts
