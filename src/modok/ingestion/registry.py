from __future__ import annotations
from pathlib import Path

import yaml

from modok.ingestion.errors import RegistryNotFoundError

_REQUIRED_FILES = ["features.yml", "modules.yml", "errors.yml", "doc-types.yml"]


class Registry:
    """Loaded feature/module/error/doc-type registries from {repo_root}/registries/."""

    def __init__(self, repo_root: Path) -> None:
        reg_dir = repo_root / "registries"
        if not reg_dir.is_dir():
            raise RegistryNotFoundError(f"registries/ directory not found at {repo_root}")

        for fname in _REQUIRED_FILES:
            if not (reg_dir / fname).exists():
                raise RegistryNotFoundError(f"Required registry file missing: {reg_dir / fname}")

        self._features: dict = self._load(reg_dir / "features.yml").get("features", {}) or {}
        self._modules: dict = self._load(reg_dir / "modules.yml").get("modules", {}) or {}
        self._errors: dict = self._load(reg_dir / "errors.yml").get("errors", {}) or {}
        self._doc_types: dict = self._load(reg_dir / "doc-types.yml").get("doc_types", {}) or {}

    @staticmethod
    def _load(path: Path) -> dict:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    def has_feature(self, slug: str) -> bool:
        return slug in self._features

    def feature_slugs(self) -> list[str]:
        return list(self._features.keys())

    def has_module(self, slug: str) -> bool:
        return slug in self._modules

    def has_error(self, slug: str) -> bool:
        return slug in self._errors

    def has_doc_type(self, slug: str) -> bool:
        return slug in self._doc_types

    def required_fields(self, doc_type: str) -> list[str]:
        entry = self._doc_types.get(doc_type, {})
        if not isinstance(entry, dict):
            return []
        return list(entry.get("required_fields", []))

    def source_files_for_feature(self, slug: str) -> list[str]:
        entry = self._features.get(slug, {})
        return entry.get("source_files", []) if isinstance(entry, dict) else []

    def test_files_for_feature(self, slug: str) -> list[str]:
        entry = self._features.get(slug, {})
        return entry.get("test_files", []) if isinstance(entry, dict) else []

    def specs_for_feature(self, slug: str) -> str | None:
        entry = self._features.get(slug, {})
        return entry.get("specs") if isinstance(entry, dict) else None

    def features_for_source_file(self, repo_path: str) -> list[str]:
        """Return feature slugs that list the given path in source_files."""
        return [
            slug for slug, entry in self._features.items()
            if isinstance(entry, dict) and repo_path in entry.get("source_files", [])
        ]

    def modules_covering_path(self, repo_path: str) -> list[str]:
        """Return module slugs whose source_roots or source_files cover the given path."""
        matches = []
        for slug, entry in self._modules.items():
            if not isinstance(entry, dict):
                continue
            matched = any(
                repo_path.startswith(root.rstrip("/") + "/") or repo_path == root
                for root in entry.get("source_roots", [])
            )
            if not matched:
                matched = repo_path in entry.get("source_files", [])
            if matched:
                matches.append(slug)
        return matches

    def features_for_module(self, module_slug: str) -> list[str]:
        """Return feature slugs that reference the given module slug."""
        matches = []
        for slug, entry in self._features.items():
            if not isinstance(entry, dict):
                continue
            if module_slug in entry.get("modules", []):
                matches.append(slug)
        return matches
