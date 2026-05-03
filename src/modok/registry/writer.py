# @spec RP-WRITE-003, RP-WRITE-004, RP-WRITE-005, RP-WRITE-006, RP-WRITE-008
from __future__ import annotations

from pathlib import Path

import yaml


def write_features_yml(path: Path, features: dict) -> None:
    """Write features registry. features is {slug: {name, description}}."""
    data = {"features": features}
    path.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def write_modules_yml(path: Path, modules: dict) -> None:
    """Write modules registry. modules is {slug: {name, description}}."""
    data = {"modules": modules}
    path.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def write_errors_yml(path: Path, errors: dict) -> None:
    """Write errors registry. errors is {slug: {normalized_error, description}}."""
    data = {"errors": errors}
    path.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def write_features_raw_yml(path: Path, features: dict) -> None:
    """Write features checkpoint. Same structure as features.yml."""
    write_features_yml(path, features)


def write_modules_raw_yml(path: Path, modules: dict) -> None:
    """Write modules checkpoint. Same structure as modules.yml."""
    write_modules_yml(path, modules)


def write_errors_raw_yml(path: Path, errors: dict) -> None:
    """Write errors checkpoint. Same structure as errors.yml."""
    write_errors_yml(path, errors)


def write_elements_yml(path: Path, elements: dict[str, list[str]]) -> None:
    """Write elements registry. elements is {module_slug: [identifier, ...]}."""
    data = {"elements": elements}
    path.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
