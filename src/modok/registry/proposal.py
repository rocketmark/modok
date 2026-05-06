# @spec RP-ENRICH-001, RP-ENRICH-002, RP-ENRICH-003, RP-ENRICH-004, RP-ENRICH-005,
#        RP-ENRICH-009, RP-ENRICH-010, RP-NORM-001, RP-WRITE-008, RP-WRITE-009,
#        RP-RPT-001, RP-RPT-002
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from modok.llm.errors import LLMResponseError, LLMUnavailableError
from modok.llm.gateway import enrich_section  # noqa: E402 — patched by tests


@dataclass
class EnrichSectionResult:
    features: list[str] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)
    error_signatures: list[str] = field(default_factory=list)
    known_issues: list[str] = field(default_factory=list)
    failure_modes: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    observation_events: list[str] = field(default_factory=list)


@dataclass
class ProposalSummary:
    sections_processed: int
    sections_failed: int
    docs_processed: int
    docs_skipped: int
    entries_written: dict
    failed_sections: list


_CANDIDATE_FIELDS = [
    "features",
    "modules",
    "error_signatures",
    "known_issues",
    "failure_modes",
    "decisions",
    "observation_events",
]


def _merge_candidates(results: list[EnrichSectionResult]) -> dict:
    """Merge per-section results into one flat list per type, deduplicated by exact string."""
    merged: dict[str, list[str]] = {f: [] for f in _CANDIDATE_FIELDS}
    seen: dict[str, set[str]] = {f: set() for f in _CANDIDATE_FIELDS}

    for result in results:
        for f in _CANDIDATE_FIELDS:
            for item in getattr(result, f):
                if item not in seen[f]:
                    seen[f].add(item)
                    merged[f].append(item)

    return merged


def propose_registries(repo_root: Path, cfg) -> ProposalSummary:
    from modok.registry.discovery import discover_docs
    from modok.registry.parser import parse_sections
    from modok.registry.slugify import resolve_slug_collisions
    from modok.registry.writer import (
        write_errors_raw_yml,
        write_features_raw_yml,
        write_modules_raw_yml,
    )

    docs, _ = discover_docs(repo_root)

    doc_section_map: dict = {}
    all_sections: list = []
    for doc in docs:
        sections = parse_sections(doc)
        doc_section_map[doc] = sections
        all_sections.extend(sections)

    total_sections = len(all_sections)
    total_docs = len(docs)

    # RP-ENRICH-003
    print(
        f"Found {total_sections} section{'s' if total_sections != 1 else ''} across "
        f"{total_docs} doc{'s' if total_docs != 1 else ''}",
        file=sys.stderr,
    )

    docs_processed = sum(1 for doc in docs if doc_section_map.get(doc))
    docs_skipped = sum(1 for doc in docs if not doc_section_map.get(doc))

    section_results: list[EnrichSectionResult] = []
    failed_sections: list = []
    sections_processed = 0
    sections_failed = 0

    for idx, section in enumerate(all_sections, start=1):
        try:
            result = enrich_section(section, cfg.llm)
            section_results.append(result)
            sections_processed += 1
            # RP-ENRICH-010: one progress line per section
            print(f"{idx}/{total_sections}", file=sys.stderr)
        except (LLMUnavailableError, LLMResponseError) as exc:
            failed_sections.append((section.heading, section.doc_path))
            sections_failed += 1
            # RP-ENRICH-010: failure appears inline on the progress line
            print(f"{idx}/{total_sections} FAILED: '{section.heading}' ({exc})", file=sys.stderr)

    # RP-NORM-001: merge per-section candidates before writing checkpoint files
    merged = _merge_candidates(section_results)

    raw_features = merged.get("features", [])
    raw_modules = merged.get("modules", [])
    raw_errors = merged.get("error_signatures", [])

    features_entries = resolve_slug_collisions(
        [{"name": n, "description": ""} for n in raw_features],
        registry_file="features.raw.yml",
    )
    modules_entries = resolve_slug_collisions(
        [{"name": n, "description": ""} for n in raw_modules],
        registry_file="modules.raw.yml",
    )
    errors_raw_entries = resolve_slug_collisions(
        [{"name": e, "description": ""} for e in raw_errors],
        registry_file="errors.raw.yml",
    )
    errors_entries = {
        slug: {"normalized_error": entry["name"], "description": entry["description"]}
        for slug, entry in errors_raw_entries.items()
    }

    # RP-WRITE-008: write .raw.yml checkpoint files
    reg_dir = repo_root / "registries"
    reg_dir.mkdir(parents=True, exist_ok=True)

    write_features_raw_yml(reg_dir / "features.raw.yml", features_entries)
    write_modules_raw_yml(reg_dir / "modules.raw.yml", modules_entries)
    write_errors_raw_yml(reg_dir / "errors.raw.yml", errors_entries)

    # RP-WRITE-009: do NOT write final .yml files here

    return ProposalSummary(
        sections_processed=sections_processed,
        sections_failed=sections_failed,
        docs_processed=docs_processed,
        docs_skipped=docs_skipped,
        entries_written={
            "features.raw.yml": len(features_entries),
            "modules.raw.yml": len(modules_entries),
            "errors.raw.yml": len(errors_entries),
        },
        failed_sections=failed_sections,
    )
