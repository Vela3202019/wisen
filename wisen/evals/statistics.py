"""Small, deterministic resampling and agreement statistics for eval reports."""
import math
import random
from collections.abc import Callable, Sequence
from statistics import mean
from typing import TypeVar

T = TypeVar("T")


def bootstrap_ci(rows: Sequence[T], statistic: Callable[[Sequence[T]], float | None], *,
                 seed: int = 0, samples: int = 2000) -> list[float] | None:
    if not rows:
        return None
    rng = random.Random(seed)
    estimates = sorted(v for _ in range(samples)
                       if (v := statistic(rng.choices(rows, k=len(rows)))) is not None)
    if not estimates:
        return None
    last = len(estimates) - 1
    return [estimates[int(last * 0.025)], estimates[math.ceil(last * 0.975)]]


def paired_summary(deltas: Sequence[float]) -> dict:
    """Resample cases, not runs; sign test on non-tied case-level differences."""
    wins = sum(delta > 0 for delta in deltas)
    losses = sum(delta < 0 for delta in deltas)
    n = wins + losses
    p = min(1.0, 2 * sum(math.comb(n, i) / 2**n for i in range(min(wins, losses) + 1))) if n else None
    return {"cases": len(deltas), "difference": mean(deltas) if deltas else None,
            "ci95": bootstrap_ci(deltas, mean), "wins": wins, "losses": losses,
            "ties": len(deltas) - n, "sign_p": p}
