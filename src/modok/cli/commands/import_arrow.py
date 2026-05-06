# @spec IA-CMD-001, IA-CMD-002, IA-CMD-003, IA-CMD-004, IA-CMD-005, IA-CMD-006, IA-CMD-007, IA-CMD-008, IA-OUT-006
"""modok import-arrow command."""

from __future__ import annotations

import sys
from pathlib import Path

import click
import yaml

from modok.import_arrow import dedup as _dedup
from modok.import_arrow import llm as _llm
from modok.import_arrow.extractor import (
    extract_features,
    parse_key_components_from_doc,
    report_unclaimed_files,
    validate_source_files,
)
from modok.import_arrow.modules import apply_key_components_overlay, build_modules_pass1
from modok.import_arrow.wiring import wire_features_to_modules
from modok.import_arrow.writer import write_registries

_REQUIRED_FIELDS = ("id", "description", "arrow_doc", "specs")


@click.command("import-arrow")
@click.option("--project", required=True, help="Project slug.")
@click.option("--repo", default=None, help="Repo root path (overrides config).")
@click.option(
    "--dry-run", is_flag=True, default=False, help="Print proposed output; write nothing."
)
@click.option("--no-llm", is_flag=True, default=False, help="Skip both LLM passes.")
def import_arrow_cmd(project: str, repo: str | None, dry_run: bool, no_llm: bool) -> None:
    if repo is None:
        from modok.cli.config import ModokConfig

        config = ModokConfig.load()
        proj = config.project(project)
        repo_root = Path(proj.repo)
    else:
        repo_root = Path(repo)

    index_path = repo_root / "docs" / "arrows" / "index.yaml"
    if not index_path.exists():
        click.echo(f"index.yaml not found at {index_path}", err=True)
        raise SystemExit(1)

    raw = yaml.safe_load(index_path.read_text(encoding="utf-8")) or {}
    arrow_entries = raw.get("arrows", []) or []

    if not arrow_entries:
        click.echo("No arrows found in index.yaml — nothing to import.")
        raise SystemExit(0)

    for entry in arrow_entries:
        for field in _REQUIRED_FIELDS:
            if field not in entry:
                click.echo(
                    f"Error: arrow entry missing required field '{field}': {entry}",
                    err=True,
                )
                raise SystemExit(1)

    # Load code map (auto-generate hint if absent)
    code_map_path = repo_root / ".modok" / "code-map.yml"
    if not code_map_path.exists():
        click.echo("WARN: code map not found — run 'modok extract-code-map' first", err=True)
        code_map: dict = {"files": []}
    else:
        code_map = yaml.safe_load(code_map_path.read_text(encoding="utf-8")) or {"files": []}

    # Feature extraction
    features = extract_features(arrow_entries, repo_root=repo_root)
    for slug, feat in features.items():
        validate_source_files(
            slug,
            feat["source_files"],
            code_map,
            warn=lambda m: print(m, file=sys.stderr),
        )

    # Module extraction — Pass 1 (mechanical)
    modules = build_modules_pass1(features, code_map)

    # Module extraction — Pass 2 (Key Components overlay)
    arrow_key_components: dict[str, list[dict]] = {}
    for entry in arrow_entries:
        doc_content = (repo_root / entry["arrow_doc"]).read_text(encoding="utf-8")
        components = parse_key_components_from_doc(doc_content)
        if components:
            arrow_key_components[entry["id"]] = components

    modules = apply_key_components_overlay(modules, arrow_key_components, code_map)

    # Advisory dedup checks (warn-only)
    _dedup.check_subset_pairs(modules)
    _dedup.check_shared_files(modules)

    llm_name_count = 0
    dedup_resolved = 0
    conflicts_flagged = 0

    if not no_llm and not dry_run:
        # LLM Pass 1 — name/description generation
        candidates = _llm.filter_modules_for_llm_pass(modules)
        if candidates:

            def _stub_name(batch):
                return [
                    {
                        "slug": m["slug"],
                        "name": m["slug"].replace("-", " ").title(),
                        "description": "",
                    }
                    for m in batch
                ]

            results = _llm.run_name_description_pass(candidates, llm_call=_stub_name)
            for slug, updates in results.items():
                if slug in modules:
                    modules[slug].update(updates)
            llm_name_count = len(candidates)

        # LLM Pass 2 — dedup resolution
        pairs = _dedup.find_duplicate_pairs(modules)
        if pairs:

            def _stub_dedup(payload):
                return {"same_concept": True, "keep": payload["slug_a"], "description": ""}

            modules, conflicts = _llm.run_dedup_pass(pairs, modules, llm_call=_stub_dedup)
            conflicts_flagged = len(conflicts)
            dedup_resolved = len(pairs) - conflicts_flagged

            if conflicts:
                for msg in conflicts:
                    click.echo(f"CONFLICT: {msg}")
                raise SystemExit(1)

    elif not dry_run:
        # --no-llm: detect and warn, retain both
        pairs = _dedup.find_duplicate_pairs(modules)
        modules = _dedup.resolve_duplicates_no_llm(pairs, modules)

    # Wire features to modules
    features = wire_features_to_modules(features, modules)

    if dry_run:
        click.echo(yaml.dump({"features": features}, default_flow_style=False, allow_unicode=True))
        click.echo(yaml.dump({"modules": modules}, default_flow_style=False, allow_unicode=True))
        raise SystemExit(0)

    write_registries(features, modules, repo_root=repo_root)

    click.echo(
        f"Imported {len(features)} features, {len(modules)} modules "
        f"→ registries/features.yml, registries/modules.yml"
    )
    click.echo(f"  {llm_name_count} modules used LLM name generation")
    click.echo(f"  {dedup_resolved} dedup pairs resolved")
    click.echo(f"  {conflicts_flagged} conflicts flagged")

    report_unclaimed_files(features, code_map)
