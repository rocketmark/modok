# @spec IA-OUT-001, IA-OUT-002, IA-OUT-003, IA-OUT-004, IA-OUT-005
"""Output writing for features.yml and modules.yml."""
from __future__ import annotations

from pathlib import Path

import yaml


def write_registries(features: dict, modules: dict, repo_root: Path) -> None:
    """Write registries/features.yml and registries/modules.yml to repo_root."""
    reg_dir = repo_root / "registries"
    reg_dir.mkdir(parents=True, exist_ok=True)

    (reg_dir / "features.yml").write_text(
        yaml.dump({"features": features}, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    (reg_dir / "modules.yml").write_text(
        yaml.dump({"modules": modules}, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
