# @spec RP-SLUG-001, RP-SLUG-002, RP-SLUG-003, RP-SLUG-004
from __future__ import annotations

import re
import sys
import unicodedata


def slugify(text: str) -> str:
    text = _strip_leading_symbols(text)
    text = text.replace("(s)", "s")
    text = re.sub(r"[().,;:!?\"'`@#$%^&*+=<>{}\[\]\\|/~]", "", text)
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    if len(text) > 40:
        truncated = text[:40]
        last_dash = truncated.rfind("-")
        if last_dash > 0:
            truncated = truncated[:last_dash]
        text = truncated.strip("-")
    return text


def _strip_leading_symbols(text: str) -> str:
    """Strip leading Unicode non-ASCII symbol characters and whitespace."""
    i = 0
    while i < len(text):
        c = text[i]
        cat = unicodedata.category(c)
        if cat[0] == "S" and ord(c) > 127:
            i += 1
        elif c in " \t\n\r":
            i += 1
        else:
            break
    return text[i:]


def resolve_slug_collisions(entries: list[dict]) -> dict:
    """Apply slug collision rules (RP-SLUG-003, RP-SLUG-004) to a list of name-keyed entries.

    Each entry must have a "name" key. Returns a slug-keyed dict.
    """
    result: dict = {}

    for entry in entries:
        name = entry["name"]
        slug = slugify(name) or "unnamed"

        if slug not in result:
            result[slug] = entry
        else:
            existing = result[slug]
            existing_norm = existing["name"].lower().strip()
            new_norm = name.lower().strip()

            if existing_norm == new_norm:
                # RP-SLUG-003: identical lowercased names — merge, keep the longer original
                if len(name) > len(existing["name"]):
                    print(f"merged duplicate slug '{slug}': kept '{name}'", file=sys.stderr)
                    result[slug] = entry
                else:
                    print(
                        f"merged duplicate slug '{slug}': kept '{existing['name']}'",
                        file=sys.stderr,
                    )
            else:
                # RP-SLUG-004: genuinely different names — keep both, second gets -2 suffix
                suffix_slug = f"{slug}-2"
                print(
                    f"slug collision '{slug}': could not merge — review registries/",
                    file=sys.stderr,
                )
                result[suffix_slug] = entry

    return result
