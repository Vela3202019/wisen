"""
Run the replay suite: every confirmed case, under config A vs config B, judged.
A case "passes" under a config when the agent does NOT repeat the corrected
behavior. Reports land in evals/reports/.

`compare` is the same suite for a Gardener proposal: the current file against
the proposed one, on the eval cases and the clean set, capped per proposal,
with what each replay cost (docs/plan/cloud-architecture.md §5.4).
"""
import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from wisen.evals.cases import Case, load_cases
from wisen.evals.replay import run_replay

REPORTS_DIR = Path(__file__).parent / "reports"


def _runnable(cases: list[Case]) -> list[Case]:
    return [
        c for c in cases
        if c.status == "confirmed" and c.split == "eval" and c.behavior_check
    ]


@dataclass
class Variant:
    """One config to replay under: a CLAUDE.md for the staged config dir, and
    files to put in place first (relative to ~/.claude, or to the project)."""
    claude_md: Path | None = None
    config_files: dict[str, str] = field(default_factory=dict)
    project_files: dict[str, str] = field(default_factory=dict)


async def run_case(case: Case, variant: Variant | Path | None, k: int) -> dict:
    """Replay one case under one config and judge it: {case, passed, evidence,
    tokens, cost_usd}, or {case, error}. This is the interface `compare` and
    `run_suite` call; a test passes a fake with the same shape."""
    from wisen.evals.judge import judge_with_usage  # builds a model agent

    if not isinstance(variant, Variant):
        variant = Variant(claude_md=variant)
    replay = await asyncio.to_thread(
        lambda: run_replay(case, variant.claude_md, config_files=variant.config_files,
                           project_files=variant.project_files))
    if replay.error:
        return {"case": case.id, "error": replay.error}
    verdict, judge_tokens = await judge_with_usage(case, replay, k)
    return {
        "case": case.id,
        "passed": not verdict.repeated,
        "evidence": verdict.evidence,
        "tokens": replay.tokens + judge_tokens,
        "cost_usd": replay.cost_usd,
    }


RunCase = Callable[[Case, Variant, int], Awaitable[dict]]


def _rate(results: list[dict]) -> float | None:
    scored = [r for r in results if "passed" in r]
    return round(sum(r["passed"] for r in scored) / len(scored), 3) if scored else None


async def compare(cases: list[Case], current: Variant, proposed: Variant, *, cap: int, k: int = 3,
                  run: RunCase = run_case) -> dict:
    """Replay up to `cap` confirmed eval cases and `cap` clean-set cases under
    the current and the proposed config. The accept rule (`meets_rule`): the
    eval pass rate rises and the clean-set pass rate does not drop. None when
    there is nothing to judge it on."""
    confirmed = [c for c in cases if c.status == "confirmed" and c.split == "eval" and c.behavior_check]
    picked = {"eval": [c for c in confirmed if c.kind == "correction"][:cap],
              "clean": [c for c in confirmed if c.kind == "clean"][:cap]}
    report: dict = {"cases": {kind: len(cs) for kind, cs in picked.items()}, "replays": 0,
                    "tokens": 0, "cost_usd": None}
    for name, variant in (("current", current), ("proposed", proposed)):
        rates = {}
        for kind, kind_cases in picked.items():
            results = [await run(c, variant, k) for c in kind_cases]
            for r in results:
                report["replays"] += 1
                report["tokens"] += r.get("tokens") or 0
                if r.get("cost_usd") is not None:
                    report["cost_usd"] = round((report["cost_usd"] or 0) + r["cost_usd"], 4)
            rates[kind] = _rate(results)
        report[name] = {"pass_rate": rates["eval"], "clean_pass_rate": rates["clean"]}
    before, after = report["current"], report["proposed"]
    if before["pass_rate"] is None or after["pass_rate"] is None:
        report["meets_rule"] = None
    else:
        clean_held = (before["clean_pass_rate"] is None or after["clean_pass_rate"] is None
                      or after["clean_pass_rate"] >= before["clean_pass_rate"])
        report["meets_rule"] = after["pass_rate"] > before["pass_rate"] and clean_held
    return report


async def run_suite(claude_md_a: Path | None, claude_md_b: Path | None = None,
                    k: int = 3, limit: int | None = None) -> dict:
    cases = _runnable(load_cases())
    if limit:
        cases = cases[:limit]
    report: dict = {
        "ran_at": datetime.now(UTC).isoformat(),
        "cases": len(cases),
        "configs": {},
    }
    variants = {"a": claude_md_a}
    if claude_md_b is not None:
        variants["b"] = claude_md_b
    for name, claude_md in variants.items():
        results = [await run_case(c, claude_md, k) for c in cases]
        report["configs"][name] = {
            "claude_md": str(claude_md) if claude_md else "(current)",
            "pass_rate": _rate(results),
            "results": results,
        }
    REPORTS_DIR.mkdir(exist_ok=True)
    out = REPORTS_DIR / f"{report['ran_at'][:19].replace(':', '-')}.json"
    out.write_text(json.dumps(report, indent=2))
    report["report_path"] = str(out)
    return report
