"""
Tier-1 outcome metrics — deterministic, computed from transcripts and exported
reflections. No LLM involved.

Per day:
  corrections per 100 user turns (by detector type)
Across days:
  friction half-life  — mean days_persisting of resolved frictions in reflections
  alignment correlation — Pearson r between reflection alignment_score and the
  measured correction rate (evals the synthesizer's scoring against reality)
"""
import json
import math
from collections import defaultdict
from pathlib import Path

from wisen.evals.miner import scan_all, _CLAUDE_DIR

REFLECTION_DIR = Path.home() / ".wisen" / "exports" / "reflections"


def daily_correction_rates(claude_dir: Path = _CLAUDE_DIR,
                           since: str | None = None) -> dict[str, dict]:
    """date → {user_turns, corrections, per_100_turns, by_detector}"""
    days: dict[str, dict] = defaultdict(
        lambda: {"user_turns": 0, "corrections": 0, "by_detector": defaultdict(int)}
    )
    for scan in scan_all(claude_dir, since):
        d = days[scan.date]
        d["user_turns"] += scan.user_turns
        d["corrections"] += len(scan.cases)
        for c in scan.cases:
            d["by_detector"][c.detector] += 1
    out = {}
    for date, d in sorted(days.items()):
        turns = d["user_turns"]
        out[date] = {
            "user_turns": turns,
            "corrections": d["corrections"],
            "per_100_turns": round(d["corrections"] / turns * 100, 2) if turns else 0.0,
            "by_detector": dict(d["by_detector"]),
        }
    return out


def load_reflections(reflection_dir: Path = REFLECTION_DIR) -> dict[str, dict]:
    """date → reflection dict, from the exported JSON files."""
    logs = {}
    if not reflection_dir.exists():
        return logs
    for f in sorted(reflection_dir.glob("????-??-??.json")):
        try:
            logs[f.stem] = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
    return logs


def friction_half_life(logs: dict[str, dict]) -> float | None:
    """Mean days_persisting of frictions marked resolved. None if no data."""
    resolved = [
        fd.get("days_persisting", 0)
        for log in logs.values()
        for fd in log.get("friction_deltas", [])
        if fd.get("status") == "resolved"
    ]
    return round(sum(resolved) / len(resolved), 2) if resolved else None


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return round(cov / (sx * sy), 3)


def alignment_correlation(rates: dict[str, dict], logs: dict[str, dict]) -> dict:
    """Pearson r between alignment_score and correction rate on shared dates.
    A healthy synthesizer scores LOW alignment on high-correction days → r < 0."""
    shared = sorted(set(rates) & set(logs))
    xs = [logs[d].get("alignment_score", 0.0) for d in shared]
    ys = [rates[d]["per_100_turns"] for d in shared]
    return {"dates": len(shared), "pearson_r": pearson(xs, ys)}


def report(claude_dir: Path = _CLAUDE_DIR, reflection_dir: Path = REFLECTION_DIR,
           since: str | None = None) -> dict:
    rates = daily_correction_rates(claude_dir, since)
    logs = load_reflections(reflection_dir)
    return {
        "daily": rates,
        "friction_half_life_days": friction_half_life(logs),
        "alignment_vs_corrections": alignment_correlation(rates, logs),
    }
