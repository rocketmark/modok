from __future__ import annotations
from dataclasses import dataclass


@dataclass
class ConfidenceBand:
    score: float
    low: float
    high: float


def confidence_band(
    base: float,
    boosts: list[float] | None = None,
    penalties: list[float] | None = None,
    uncertainty: float = 0.06,
) -> ConfidenceBand:
    raise NotImplementedError
