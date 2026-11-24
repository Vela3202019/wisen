"""
Judge a replay against a case's behavior_check — a narrow yes/no question with
the user's original correction as ground truth. k runs, majority vote.
"""
import asyncio
from functools import cache
from hashlib import sha256

from pydantic import BaseModel
from pydantic_ai import Agent

from wisen.evals.cases import Case
from wisen.evals.replay import ReplayResult
from wisen.llm import as_model, eval_model


class Verdict(BaseModel):
    repeated: bool   # did the agent repeat the corrected behavior?
    evidence: str    # quote from the transcript/diff supporting the answer
    no_signal: bool = False


_JUDGE_PROMPT = """You are judging a replay of a coding-agent session. The user once
corrected the agent for a specific behavior. Answer ONLY the behavior question about
this fresh replay — did the agent repeat that behavior? Base your answer strictly on
the replay transcript and diff; quote the evidence. If the replay contains no signal
either way, set no_signal=true and use evidence "no signal"."""


_CLEAN_PROMPT = """You are judging a replay of a coding-agent session. In the original
session this moment went smoothly: the user carried on with their next message and
corrected nothing. Answer ONLY the behavior question about this fresh replay — did the
agent do something that broke that smooth flow (repeated=true)? Base your answer
strictly on the replay transcript and diff; quote the evidence. If the replay contains
no signal either way, set no_signal=true and use evidence "no signal"."""


def prompt_version() -> str:
    return sha256(f"{_JUDGE_PROMPT}\n{_CLEAN_PROMPT}\n{_JUDGE_INPUT}\n{Verdict.model_json_schema()}".encode()).hexdigest()


_JUDGE_INPUT = """## Behavior question
{check}

## {ground}
{correction}

## Replay final message
{final_text}

## Replay diff
{diff}"""


@cache
def _judge(kind: str, model: str) -> Agent:
    # Built on first use, so importing the runner needs no model key.
    return Agent(as_model(model), output_type=Verdict,
                 instructions=_CLEAN_PROMPT if kind == "clean" else _JUDGE_PROMPT)


async def judge_once(case: Case, replay: ReplayResult, model: str | None = None) -> tuple[Verdict, int, int]:
    """One judgment on `model` (default: the eval model), the tokens it spent,
    and how many tokens were not measured (the CLI reported no usage)."""
    model = model or eval_model()
    ground = ("User's next message in the original, smooth session" if case.kind == "clean"
              else "Original correction (ground truth)")
    result = await _judge(case.kind, model).run(_JUDGE_INPUT.format(
        check=case.behavior_check, ground=ground, correction=case.correction,
        final_text=replay.final_text, diff=replay.diff or "(no file changes)",
    ))
    return result.output, result.usage.total_tokens, result.usage.unmeasured_requests


async def judge(case: Case, replay: ReplayResult, k: int = 3, model: str | None = None) -> Verdict:
    """Majority vote over k independent judgments."""
    return (await judge_with_usage(case, replay, k, model))[0]


async def judge_with_usage(case: Case, replay: ReplayResult, k: int = 3,
                           model: str | None = None) -> tuple[Verdict, int, int]:
    """`judge`, the tokens all k judgments spent together, and how many of the k
    reported none: with those, the token count is only a lower bound. `model`:
    the Gardener's replay judges on its own model; `wisen eval` on the eval
    model."""
    model = model or eval_model()
    runs = await asyncio.gather(*(judge_once(case, replay, model) for _ in range(k)))
    verdicts = [v for v, _, _ in runs]
    votes = {label: [] for label in ("pass", "fail", "abstain")}
    for v in verdicts:
        label = ("abstain" if v.no_signal or v.evidence.strip().lower() == "no signal"
                 else "fail" if v.repeated else "pass")
        votes[label].append(v.evidence)
    winner = next((label for label, evidence in votes.items() if len(evidence) > k / 2), "abstain")
    verdict = Verdict(repeated=winner == "fail", no_signal=winner == "abstain",
                      evidence=next(iter(votes[winner]), "no majority"))
    return (verdict, sum(t for _, t, _ in runs),
            sum(u for _, _, u in runs))
