"""modok extract-code-map command."""
# @spec CM-CMD-001, CM-CMD-002, CM-CMD-003, CM-CMD-004, CM-CMD-005

from __future__ import annotations

from pathlib import Path

import click

from modok.code_map.git import get_head_sha
from modok.code_map.scanner import scan_repo
from modok.code_map.writer import write_code_map


def _modok_covered_by_gitignore(gitignore: Path) -> bool:
    if not gitignore.is_file():
        return False
    for line in gitignore.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped in (".modok/", ".modok", ".modok/**"):
            return True
    return False


@click.command("extract-code-map")
@click.option("--project", required=True, help="Project slug.")
@click.option("--repo", default=None, help="Repo root path (overrides config).")
@click.option("--output", default=None, help="Output path (default: <repo>/.modok/code-map.yml).")
def extract_code_map_cmd(project: str, repo: str | None, output: str | None) -> None:
    if repo is None:
        from modok.cli.config import ModokConfig

        config = ModokConfig.load()
        proj = config.project(project)
        repo_root = Path(proj.repo)
    else:
        repo_root = Path(repo)

    if not repo_root.is_dir():
        click.echo(f"not a directory: {repo_root}", err=True)
        raise SystemExit(1)

    out_path = Path(output) if output else repo_root / ".modok" / "code-map.yml"

    entries = scan_repo(repo_root)
    git_commit = get_head_sha(repo_root)
    write_code_map(
        out_path, project=project, repo_root=repo_root, entries=entries, git_commit=git_commit
    )

    source_count = sum(1 for e in entries if e.get("role") == "source")
    test_count = sum(1 for e in entries if e.get("role") == "test")
    config_count = sum(1 for e in entries if e.get("role") == "config")
    docs_count = sum(1 for e in entries if e.get("role") == "docs")

    click.echo(
        f"Extracted code map: {len(entries)} files "
        f"({source_count} source, {test_count} test, {config_count} config, {docs_count} docs)"
        f" → {out_path}"
    )

    if not _modok_covered_by_gitignore(repo_root / ".gitignore"):
        click.echo("Tip: add '.modok/' to .gitignore to exclude the code map from version control.")
