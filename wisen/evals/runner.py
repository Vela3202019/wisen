"""Replay and judge cases, pairing the same cases and runs across both arms."""
import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from uuid import uuid4

from pydantic_ai.exceptions import AgentRunError, UserError

from wisen.evals.cases import Case, load_cases
from wisen.evals.manifest import Files, FrozenCase, Manifest
from wisen.evals.replay import run_replay
from wisen.evals.statistics import paired_summary
from wisen.llm import eval_model

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
    config_files: Files = field(default_factory=dict)
    project_files: Files = field(default_factory=dict)
    base_commit: str | None = None
    model: str | None = None
    harness: str | None = None
    capture: bool = False


async def run_case(case: Case, variant: Variant | Path | None, k: int, model: str | None = None) -> dict:
    """Replay one case under one config and judge it: {case, passed, evidence,
    tokens, cost_usd}, or {case, error}. This is the interface `compare` and
    `run_suite` call; a test passes a fake with the same shape. `model`: what
    the judge runs on (default: the eval model; the Gardener's replay passes
    the Gardener's own)."""
    from wisen.evals.judge import judge_with_usage  # builds a model agent

    if not isinstance(variant, Variant):
        variant = Variant(claude_md=variant)
    replay = await asyncio.to_thread(
        run_replay, case, variant.claude_md, config_files=variant.config_files,
        project_files=variant.project_files, base_commit=variant.base_commit,
        model=variant.model, harness=variant.harness)
    if replay.error:
        return {"case": case.id, "error": replay.error}
    try:
        verdict, judge_tokens, judge_unmeasured = await judge_with_usage(case, replay, k, model)
    except (AgentRunError, UserError, OSError, ValueError) as exc:
        return {"case": case.id, "error": f"judge failed: {type(exc).__name__}", "tokens": replay.tokens,
                "cost_usd": replay.cost_usd,
                **({"output": {"final_text": replay.final_text, "diff": replay.diff}} if variant.capture else {})}
    result = {
        "case": case.id,
        "outcome": "abstain" if verdict.no_signal else "fail" if verdict.repeated else "pass",
        "evidence": verdict.evidence,
        "tokens": replay.tokens + judge_tokens,
        "tokens_unmeasured": judge_unmeasured,
        "cost_usd": replay.cost_usd,
    }
    if not verdict.no_signal:
        result["passed"] = not verdict.repeated
    if variant.capture:
        result["output"] = {"final_text": replay.final_text, "diff": replay.diff}
    return result


RunCase = Callable[[Case, Variant, int], Awaitable[dict]]


def _rate(results: list[dict]) -> float | None:
    scored = [r for r in results if "passed" in r]
    return round(sum(r["passed"] for r in scored) / len(scored), 3) if scored else None


def _outcome(result: dict) -> str:
    return result.get("outcome") or ("pass" if result.get("passed") else "fail" if "passed" in result else "error")


async def compare(cases: list[Case], current: Variant, proposed: Variant, *, cap: int, k: int = 5,
                  judge_votes: int = 3, clean_margin: float = 0.05, run: RunCase = run_case) -> dict:
    """Advisory comparison for the Gardener, without frozen artifacts."""
    confirmed = [c for c in cases if c.status == "confirmed" and c.split == "eval" and c.behavior_check]
    picked = {"eval": [c for c in confirmed if c.kind == "correction"][:cap],
              "clean": [c for c in confirmed if c.kind == "clean"][:cap]}
    runs = {kind: [(case, current, proposed) for case in selected] for kind, selected in picked.items()}
    return await _paired(runs, k, judge_votes, clean_margin, run, capture=False)


async def run_manifest(manifest: Manifest, *, k: int = 5, judge_votes: int = 3, clean_margin: float = 0.05,
                       holdout: bool = False, run: RunCase | None = None) -> dict:
    from wisen.evals.judge import prompt_version

    if manifest.judge_prompt != prompt_version():
        raise ValueError("judge prompt does not match the frozen manifest")

    async def judge_run(case: Case, variant: Variant, votes: int) -> dict:
        return await run_case(case, variant, votes, manifest.judge_model)

    def variants(entry: FrozenCase):
        return tuple(Variant(config_files=arm.config,
                             project_files={**arm.project, **(entry.cumulative_project_files if updated else {})},
                             base_commit=entry.base_commit, model=manifest.model, harness=manifest.harness, capture=True)
                     for updated, arm in enumerate((manifest.current, manifest.proposed)))

    runs: dict = {}
    for entry in manifest.cases:
        if (entry.cohort == "holdout") != holdout:
            continue
        kind = "eval" if entry.cohort == "targeted" else entry.cohort
        runs.setdefault(kind, []).append((entry.case, *variants(entry)))
    report = await _paired(runs, k, judge_votes, clean_margin, run or judge_run, capture=True)
    return {**report, "manifest": manifest.digest(), "proposal_id": manifest.proposal_id, "holdout": holdout,
            "run_id": uuid4().hex}


async def _paired(runs: dict, k: int, votes: int, margin: float, run: RunCase, *, capture: bool) -> dict:
    if k < 1 or votes < 1 or not 0 <= margin < 1:
        raise ValueError("repeats and votes must be positive; clean margin must be in [0, 1)")
    arms = ("current", "proposed")
    report: dict = {
        "cases": {kind: len(cs) for kind, cs in runs.items()}, "replays": 0, "tokens": 0,
        "tokens_unmeasured": 0, "cost_usd": None, "agent_repeats": k, "judge_votes": votes,
        "clean_margin": margin, "paired": {}, "case_results": [],
    }
    rates: dict = {arm: {} for arm in arms}
    for kind, cases in runs.items():
        totals = {arm: dict.fromkeys(("pass", "fail", "abstain", "error"), 0) for arm in arms}
        deltas = []
        for case, current, proposed in cases:
            case_outcomes = {arm: totals[arm].fromkeys(totals[arm], 0) for arm in arms}
            case_deltas = []
            matched = dict.fromkeys(arms, 0)
            attempts = []
            for repeat in range(k):
                results = {}
                for arm in arms if repeat % 2 == 0 else reversed(arms):
                    result = await run(case, current if arm == "current" else proposed, votes)
                    outcome = _outcome(result)
                    case_outcomes[arm][outcome] += 1
                    totals[arm][outcome] += 1
                    results[arm] = {**result, "outcome": outcome}
                    report["replays"] += 1
                    report["tokens"] += result.get("tokens") or 0
                    report["tokens_unmeasured"] += bool(result.get("tokens_unmeasured"))
                    if result.get("cost_usd") is not None:
                        report["cost_usd"] = round((report["cost_usd"] or 0) + result["cost_usd"], 4)
                a, b = (results[arm]["outcome"] for arm in arms)
                if {a, b} <= {"pass", "fail"}:
                    case_deltas.append(int(b == "pass") - int(a == "pass"))
                    matched["current"] += a == "pass"
                    matched["proposed"] += b == "pass"
                if capture:
                    attempts.append(results)
            delta = mean(case_deltas) if case_deltas else None
            if delta is not None:
                deltas.append(delta)
            case_result = {"case": case.id, "cohort": kind, "outcomes": case_outcomes,
                           "pass_rates": {arm: case_outcomes[arm]["pass"] / k for arm in arms},
                           "matched_scored": len(case_deltas), "difference": delta,
                           "matched_pass_rates": ({arm: matched[arm] / len(case_deltas) for arm in arms}
                                                  if case_deltas else None)}
            if capture:
                case_result["attempts"] = attempts
            report["case_results"].append(case_result)
        for arm in arms:
            rates[arm][kind] = totals[arm]["pass"] / (len(cases) * k) if cases else None
        report["paired"][kind] = {**paired_summary(deltas), "outcomes": totals}
    for arm in arms:
        report[arm] = {"pass_rate": rates[arm].get("eval"), "clean_pass_rate": rates[arm].get("clean")}
    targeted = report["paired"].get("eval", {})
    clean = report["paired"].get("clean", {})
    if not targeted.get("ci95") or not clean.get("ci95"):
        report["meets_rule"] = None
    else:
        all_scored = all(not counts["error"] and not counts["abstain"]
                         for cohort in (targeted, clean) for counts in cohort["outcomes"].values())
        report["meets_rule"] = all_scored and targeted["ci95"][0] > 0 and clean["ci95"][0] >= -margin
    return report


async def run_suite(claude_md_a: Path | None, claude_md_b: Path | None = None,
                    k: int = 3, limit: int | None = None) -> dict:
    """The suite per config, judged on the eval model: refused up front when the
    configured model is the subscription's, before any replay runs."""
    api_model = eval_model()
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
        results = [await run_case(c, claude_md, k, api_model) for c in cases]
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
