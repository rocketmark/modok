"""modok ingest-elements — extract code identifiers from source files into elements.yml."""

from __future__ import annotations

from pathlib import Path

import click

from modok.cli.config import ModokConfig
from modok.ingestion.registry import Registry
from modok.registry.writer import write_elements_yml, write_modules_yml
from modok.retrieval.elements import extract_module_elements


@click.command()
@click.option("--project", required=True, help="Project slug from modok.toml")
def ingest_elements_cmd(project: str) -> None:
    """Extract code identifiers from each module's source files and write to elements.yml.

    Run this after adding or changing source files so that modok retrieve can
    identify modules from ticket language (e.g. 'reinit button' → device-card).
    """
    config = ModokConfig.load()
    proj = config.project(project)
    repo_root = Path(proj.repo)

    registry = Registry(repo_root)
    reg_dir = repo_root / "registries"

    elements: dict[str, list[str]] = {}
    updated = 0

    for slug, entry in registry._modules.items():
        source_files = entry.get("source_files", [])

        # Gather test files from every feature that includes this module.
        test_files: list[str] = []
        seen_test: set[str] = set()
        for feat_slug in registry.features_for_module(slug):
            for tf in registry.test_files_for_feature(feat_slug):
                if tf not in seen_test:
                    seen_test.add(tf)
                    test_files.append(tf)

        if not source_files and not test_files:
            continue
        extracted = extract_module_elements(source_files, repo_root, test_files=test_files)
        if extracted:
            elements[slug] = extracted
            updated += 1

    write_elements_yml(reg_dir / "elements.yml", elements)

    # Strip any elements keys previously written into modules.yml
    modules = {
        slug: {k: v for k, v in entry.items() if k != "elements"}
        for slug, entry in registry._modules.items()
    }
    if any("elements" in entry for entry in registry._modules.values()):
        write_modules_yml(reg_dir / "modules.yml", modules)
        click.echo("ingest-elements: removed stale elements keys from modules.yml")

    click.echo(f"ingest-elements: wrote {updated} modules to {reg_dir / 'elements.yml'}")
