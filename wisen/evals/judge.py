"""
Judge a replay against a case's behavior_check — a narrow yes/no question with
the user's original correction as ground truth. k runs, majority vote.
"""
import asyncio
from functools import cache

from pydantic import BaseModel
from pydantic_ai import Agent

from wisen.evals.cases import Case
from wisen.evals.replay import ReplayResult
from wisen.llm import get_model


class Verdict(BaseModel):
    repeated: bool   # did the agent repeat the corrected behavior?
    evidence: str    # quote from the transcript/diff supporting the answer


_JUDGE_PROMPT = """You are judging a replay of a coding-agent session. The user once
corrected the agent for a specific behavior. Answer ONLY the behavior question about
this fresh replay — did the agent repeat that behavior? Base your answer strictly on
the replay transcript and diff; quote the evidence. If the replay contains no signal
either way, answer repeated=false with evidence "no signal"."""


@cache
def _judge() -> Agent:
    # Built on first use, so importing the runner needs no model key.
    return Agent(get_model(), output_type=Verdict, instructions=_JUDGE_PROMPT)


async def judge_once(case: Case, replay: ReplayResult) -> tuple[Verdict, int]:
    """One judgment and the tokens it spent."""
    result = await _judge().run(
        f"## Behavior question\n{case.behavior_check}\n\n"
        f"## Original correction (ground truth)\n{case.correction}\n\n"
        f"## Replay final message\n{replay.final_text}\n\n"
        f"## Replay diff\n{replay.diff or '(no file changes)'}"
    )
    return result.output, result.usage().total_tokens


async def judge(case: Case, replay: ReplayResult, k: int = 3) -> Verdict:
    """Majority vote over k independent judgments."""
    return (await judge_with_usage(case, replay, k))[0]


async def judge_with_usage(case: Case, replay: ReplayResult, k: int = 3) -> tuple[Verdict, int]:
    """`judge`, and the tokens all k judgments spent together."""
    runs = await asyncio.gather(*(judge_once(case, replay) for _ in range(k)))
    verdicts = [v for v, _ in runs]
    repeated_votes = sum(v.repeated for v in verdicts)
    majority = repeated_votes > k / 2
    evidence = next((v.evidence for v in verdicts if v.repeated == majority), "")
    return Verdict(repeated=majority, evidence=evidence), sum(t for _, t in runs)
