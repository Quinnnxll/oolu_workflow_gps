from __future__ import annotations

_NEUTRAL_SCORE = 3.0


def mu_from_ratings(average_score: float, count: int, *, mu_max: float = 2.0) -> float:
    if count <= 0:
        return 1.0
    return min(mu_max, max(0.0, average_score / _NEUTRAL_SCORE))
