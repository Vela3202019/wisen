"""
`wisen eval demo`: the paired replay protocol on a public, synthetic case bank
(`demo/cases.json`), so anyone can see and reproduce a scorecard without a
private transcript. The current arm has no instructions; the proposed arm adds
`demo/rules.md`. Targeted and clean cohorts feed the gate; holdout prompts
(same rules, different wording) measure transfer.

Without a model the agent is simulated: no model call, made-up pass rates, the
real statistics and report. With `local:NAME` it replays on an OpenAI-compatible
server; with a pinned Claude model it replays through `claude -p` in a scratch
repo. Both judge on the eval model.
"""
import json
import random
import subprocess
import tempfile
from pathlib import Path

from wisen.evals.cases import Case, ContextTurn
from wisen.evals.replay import is_local
from wisen.evals.runner import RunCase, Variant, _paired, run_case

DEMO_DIR = Path(__file__).parent / "demo"
_COHORT_KIND = {"targeted": "eval", "clean": "clean", "holdout": "holdout"}
# Simulated chance a replay passes, per (cohort, rules applied).
_SIMULATED = {("targeted", False): 0.35, ("targeted", True): 0.8,
              ("holdout", False): 0.35, ("holdout", True): 0.6,
              ("clean", False): 0.9, ("clean", True): 0.9}


def rules() -> str:
    return (DEMO_DIR / "rules.md").read_text()


def load_bank() -> list[tuple[str, str, Case]]:
    """(cohort, rule type, case) for each demo case."""
    bank = []
    for item in json.loads((DEMO_DIR / "cases.json").read_text()):
        cohort = item["cohort"]
        clean = cohort == "clean"
        bank.append((cohort, item["rule_type"], Case(
            id=item["id"], date="2026-01-01", session_id="demo", source_path="synthetic",
            project_path="synthetic", detector=None if clean else "explicit-correction",
            context=[ContextTurn(role="user", content=item["prompt"])], correction=item["correction"],
            behavior_check=item["behavior_check"], split="holdout" if cohort == "holdout" else "eval",
            status="confirmed", kind="clean" if clean else "correction")))
    return bank


def simulated(seed: int = 0) -> RunCase:
    rng = random.Random(seed)
    cohorts = {case.id: cohort for cohort, _, case in load_bank()}

    async def run(case: Case, variant: Variant, votes: int) -> dict:
        ruled = bool(variant.config_files.get("CLAUDE.md"))
        return {"case": case.id, "passed": rng.random() < _SIMULATED[(cohorts[case.id], ruled)]}
    return run


def _scratch_repo(root: Path) -> tuple[Path, str]:
    project = root / "project"
    project.mkdir()
    git = ["git", "-C", str(project), "-c", "user.name=wisen demo", "-c", "user.email=demo@wisen.invalid"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "demo"], check=True)
    sha = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    return project, sha


async def run_demo(model: str | None = None, *, repeats: int = 3, votes: int = 3, clean_margin: float = 0.05,
                   judge_model: str | None = None, seed: int = 0, run: RunCase | None = None) -> dict:
    bank = load_bank()
    if model is None:
        run = run or simulated(seed)
    elif run is None:
        from wisen.llm import eval_model

        judge_model = judge_model or eval_model()

        async def run(case: Case, variant: Variant, k: int) -> dict:
            return await run_case(case, variant, k, judge_model)
    with tempfile.TemporaryDirectory() as tmp:
        base_commit = None
        if model is not None and not is_local(model):
            project, base_commit = _scratch_repo(Path(tmp))
            bank = [(cohort, rule, case.model_copy(update={"project_path": str(project)}))
                    for cohort, rule, case in bank]
        arms = [Variant(config_files=files, model=model, base_commit=base_commit, capture=True)
                for files in ({}, {"CLAUDE.md": rules()})]
        runs: dict = {}
        for cohort, _, case in bank:
            runs.setdefault(_COHORT_KIND[cohort], []).append((case, *arms))
        report = await _paired(runs, repeats, votes, clean_margin, run, capture=True)
    return {**report, "proposal_id": "demo-rules", "demo": True, "simulated": model is None,
            "model": model or "simulated", "judge_model": judge_model if model else "simulated",
            "rule_types": {case.id: rule for _, rule, case in bank}}
