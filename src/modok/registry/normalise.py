# @spec RN-CMD-001, RN-CMD-002, RN-NORM-001, RN-NORM-002, RN-NORM-003,
#        RN-CEGIS-001, RN-CEGIS-002, RN-CEGIS-003, RN-CEGIS-004,
#        RN-WRITE-001, RN-WRITE-002
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from modok.llm.errors import LLMResponseError, LLMUnavailableError
from modok.llm.gateway import normalise_candidates  # noqa: E402 — patched by tests

_ERROR_CODE_RE = re.compile(r"^[A-Z0-9_]+$")


@dataclass
class NormaliseSummary:
    fields_normalised: dict = field(default_factory=dict)
    fields_failed: list = field(default_factory=list)


def _verify_field(field_type: str, input_candidates: list, normalised: list) -> list:
    """Return list of violating entries. Empty list means verification passed."""
    violations = []

    # Empty output when input is non-empty is always a failure (context overflow / hallucinated empty)
    if not normalised and input_candidates:
        violations.append({"_empty_output": True, "reason": "LLM returned empty list for non-empty input"})
        return violations

    # RN-NORM-003 / RN-CEGIS-001: no new concepts — output count must not exceed input count
    if len(normalised) > len(input_candidates):
        excess = normalised[len(input_candidates):]
        violations.extend(excess)

    name_key = "normalized_error" if field_type == "errors" else "name"

    for entry in normalised:
        val = entry.get(name_key, "")
        # Non-empty name check
        if not val or not str(val).strip():
            violations.append(entry)
            continue
        # Error format check
        if field_type == "errors":
            if not (_ERROR_CODE_RE.match(str(val)) or (val and ord(str(val)[0]) > 127)):
                violations.append(entry)

    # Deduplicate while preserving order
    seen_ids = set()
    deduped = []
    for v in violations:
        vid = id(v) if not isinstance(v, dict) else str(v)
        if vid not in seen_ids:
            seen_ids.add(vid)
            deduped.append(v)

    return deduped


def _load_raw_candidates(raw_path: Path, field_type: str) -> list:
    data = yaml.safe_load(raw_path.read_text(encoding="utf-8")) or {}
    entries = data.get(field_type, {})
    if field_type == "errors":
        return [
            {"normalized_error": v.get("normalized_error", ""), "description": v.get("description", "")}
            for v in entries.values()
        ]
    return [
        {"name": v.get("name", ""), "description": v.get("description", "")}
        for v in entries.values()
    ]


def normalise_registries(repo_root: Path, cfg) -> NormaliseSummary:
    from modok.registry.slugify import resolve_slug_collisions
    from modok.registry.writer import write_errors_yml, write_features_yml, write_modules_yml

    registries_dir = repo_root / "registries"

    raw_file_map = {
        "features": registries_dir / "features.raw.yml",
        "modules": registries_dir / "modules.raw.yml",
        "errors": registries_dir / "errors.raw.yml",
    }

    present = {ft: p for ft, p in raw_file_map.items() if p.exists()}

    # RN-CMD-002: no raw files at all → error
    if not present:
        raise RuntimeError(
            "No .raw.yml files found in registries/. Run `modok init --assisted` first."
        )

    # RN-CMD-001: some but not all missing → warn and continue with present fields
    for ft, raw_path in raw_file_map.items():
        if ft not in present:
            print(f"Warning: {raw_path.name} not found — skipping {ft}", file=sys.stderr)

    max_repairs = getattr(cfg.llm, "cegis_max_repairs", 1)

    fields_normalised: dict = {}
    fields_failed: list = []

    writer_map = {
        "features": (write_features_yml, registries_dir / "features.yml"),
        "modules": (write_modules_yml, registries_dir / "modules.yml"),
        "errors": (write_errors_yml, registries_dir / "errors.yml"),
    }

    field_index = 0
    total_fields = len(present)

    for field_type, raw_path in present.items():
        field_index += 1
        input_candidates = _load_raw_candidates(raw_path, field_type)

        print(
            f"{field_index}/{total_fields} Normalising {field_type} ({len(input_candidates)} candidates)...",
            file=sys.stderr,
        )

        accepted = None
        counterexamples: list | None = None

        # RN-CEGIS-004: total calls ≤ max_repairs + 1
        for attempt in range(max_repairs + 1):
            if attempt > 0:
                print(
                    f"  Repair {attempt}/{max_repairs} ({len(counterexamples or [])} violation(s))...",
                    file=sys.stderr,
                )
            try:
                normalised = normalise_candidates(
                    input_candidates, field_type, cfg.llm, counterexamples=counterexamples
                )
            except (LLMUnavailableError, LLMResponseError) as exc:
                print(f"  Failed: {exc} — falling back to raw candidates", file=sys.stderr)
                fields_failed.append(field_type)
                break

            violations = _verify_field(field_type, input_candidates, normalised)

            if not violations:
                # RN-CEGIS-001: verifier passed
                accepted = normalised
                break

            # RN-CEGIS-002: verifier failed — prepare counterexamples for repair
            counterexamples = violations

            if attempt == max_repairs:
                # RN-CEGIS-003: exhausted — fall back to raw candidates
                print(
                    f"Warning: CEGIS exhausted for '{field_type}' after {max_repairs} repair "
                    f"attempt{'s' if max_repairs != 1 else ''} — falling back to raw candidates",
                    file=sys.stderr,
                )
                fields_failed.append(field_type)

        # Build final entries dict
        out_file = f"{field_type}.yml"
        if accepted is None:
            # RN-CEGIS-003: fallback — slug-resolve the raw candidates
            if field_type == "errors":
                name_entries = [
                    {"name": c.get("normalized_error", ""), "description": c.get("description", "")}
                    for c in input_candidates if c.get("normalized_error")
                ]
                resolved = resolve_slug_collisions(name_entries, registry_file=out_file)
                final_entries = {
                    slug: {"normalized_error": e["name"], "description": e["description"]}
                    for slug, e in resolved.items()
                }
            else:
                final_entries = resolve_slug_collisions(input_candidates, registry_file=out_file)
        else:
            if field_type == "errors":
                name_entries = [
                    {"name": e.get("normalized_error", ""), "description": e.get("description", "")}
                    for e in accepted if e.get("normalized_error")
                ]
                resolved = resolve_slug_collisions(name_entries, registry_file=out_file)
                final_entries = {
                    slug: {"normalized_error": e["name"], "description": e["description"]}
                    for slug, e in resolved.items()
                }
            else:
                name_entries = [
                    {"name": e.get("name", ""), "description": e.get("description", "")}
                    for e in accepted if e.get("name")
                ]
                final_entries = resolve_slug_collisions(name_entries, registry_file=out_file)

        # RN-WRITE-001: write final .yml; RN-WRITE-002: do not touch .raw.yml
        write_fn, out_path = writer_map[field_type]
        write_fn(out_path, final_entries)
        fields_normalised[field_type] = len(final_entries)

        fallback_note = " (raw fallback)" if field_type in fields_failed else ""
        print(
            f"  {field_type}: {len(input_candidates)} → {len(final_entries)} entries{fallback_note}",
            file=sys.stderr,
        )

    return NormaliseSummary(
        fields_normalised=fields_normalised,
        fields_failed=fields_failed,
    )
