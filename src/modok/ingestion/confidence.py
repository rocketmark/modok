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
    score = base
    for b in boosts or []:
        score = score + b * (1.0 - score)
    for p in penalties or []:
        score = score * (1.0 - p)
    score = max(0.0, min(1.0, score))
    low = max(0.0, score - uncertainty)
    high = min(1.0, score + uncertainty)
    return ConfidenceBand(score=score, low=low, high=high)
