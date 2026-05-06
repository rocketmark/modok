# @spec IA-WIRE-001, IA-WIRE-002, IA-WIRE-003
"""Feature → Module wiring."""

from __future__ import annotations


def wire_features_to_modules(features: dict, modules: dict) -> dict:
    """Populate each feature's 'modules' list with qualifying module slugs."""
    result = {slug: dict(entry) for slug, entry in features.items()}

    for feat_slug, feat_entry in result.items():
        feat_files = set(feat_entry.get("source_files", []))
        wired: list[str] = []

        for mod_slug, mod_entry in modules.items():
            mod_files = set(mod_entry.get("source_files", []))
            mod_roots = mod_entry.get("source_roots", [])

            if mod_roots:
                # Directory-level module: wire when any feature file is under a source_root
                for f in feat_files:
                    for root in mod_roots:
                        if f.startswith(root.rstrip("/") + "/") or f == root:
                            wired.append(mod_slug)
                            break
                    else:
                        continue
                    break
            else:
                # File-level module: non-empty subset of feature source_files
                if mod_files and mod_files.issubset(feat_files):
                    wired.append(mod_slug)

        result[feat_slug]["modules"] = wired

    return result
