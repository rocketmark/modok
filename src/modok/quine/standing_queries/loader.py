"""Standing query definitions — loaded from checked-in YAML artifacts, never
embedded as inline Python strings. See docs/llds/standing-queries.md."""
# @spec SQ-DEF-001, SQ-DEF-002, SQ-DEF-003

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

_DEFINITIONS_DIR = Path(__file__).parent


@dataclass
class StandingQueryDefinition:
    name: str
    mode: str
    pattern: str
    enrichment_query: str
    output_name: str


def load_definition(name: str) -> StandingQueryDefinition:
    path = _DEFINITIONS_DIR / f"{name.replace('-', '_')}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return StandingQueryDefinition(
        name=data["name"],
        mode=data["mode"],
        pattern=data["pattern"].strip(),
        enrichment_query=data["enrichment_query"].strip(),
        output_name=data["output_name"],
    )


def all_definitions() -> list[StandingQueryDefinition]:
    return [load_definition(p.stem) for p in sorted(_DEFINITIONS_DIR.glob("*.yaml"))]
