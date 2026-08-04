"""
Run the replay suite: every confirmed case, under config A vs config B, judged.
A case "passes" under a config when the agent does NOT repeat the corrected
behavior. Reports land in evals/reports/.
"""
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from wisen.evals.cases import Case, load_cases
from wisen.evals.judge import judge
from wisen.evals.replay import run_replay

REPORTS_DIR = Path(__file__).parent / "reports"


def _runnable(cases: list[Case]) -> list[Case]:
    return [
        c for c in cases
        if c.status == "confirmed" and c.split == "eval" and c.behavior_check
    ]


async def run_case(case: Case, claude_md: Path | None, k: int) -> dict:
    replay = await asyncio.to_thread(run_replay, case, claude_md)
    if replay.error:
        return {"case": case.id, "error": replay.error}
    verdict = await judge(case, replay, k)
    return {
        "case": case.id,
        "passed": not verdict.repeated,
        "evidence": verdict.evidence,
    }


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
        scored = [r for r in results if "passed" in r]
        report["configs"][name] = {
            "claude_md": str(claude_md) if claude_md else "(current)",
            "pass_rate": round(sum(r["passed"] for r in scored) / len(scored), 3) if scored else None,
            "results": results,
        }
    REPORTS_DIR.mkdir(exist_ok=True)
    out = REPORTS_DIR / f"{report['ran_at'][:19].replace(':', '-')}.json"
    out.write_text(json.dumps(report, indent=2))
    report["report_path"] = str(out)
    return report
