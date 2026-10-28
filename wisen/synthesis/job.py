"""
Synthesis as a durable job: one day's ingest + Observer + Synthesizer + save,
checkpointed after each stage by the job engine (wisen/jobs.py). If the
process dies after the Observer, the rerun starts at the Synthesizer. A saved
day queues that day's Gardener run (wisen/gardener/loop.py).

`synthesize_day` is the one entry for a day's run (the CLI, the launchd
script and the paired server's device job all go through it), and it is where
a day is left alone: a day with no sessions has nothing to reflect on, and a
day already reflected on is not run again unless a session came in after the
reflection or the run was explicitly asked for (`force`). The skip is
returned as a `Skipped`, which each entry point prints or reports.
"""
from dataclasses import dataclass
from datetime import UTC, datetime

from wisen.ingestion.pipeline import ingest_all, ingest_sessions
from wisen.jobs import Job, JobContext, get_engine
from wisen.llm import get_model
from wisen.store import get_store
from wisen.synthesis import agent
from wisen.synthesis.schema import DayObservation, Reflection


@dataclass
class Skipped:
    """A day the run left alone, and why."""
    date: str
    reason: str


async def run_synthesis_job(ctx: JobContext, args: dict) -> dict:
    date = args["date"]
    yesterday_doc, past_docs = await agent.load_history(date)
    model = get_model()

    async def observe_day() -> DayObservation:
        # Ingest inside the step: only the Observer's output is journaled,
        # never the raw sessions it read.
        return await agent.observe(await ingest_all(date), date, yesterday_doc, model)

    observation = await ctx.step("observe", observe_day, replay_safe=True, output=DayObservation)
    reflection = await ctx.step(
        "synthesize", lambda: agent.reflect(observation, yesterday_doc, past_docs, date, model),
        replay_safe=True, output=Reflection,
    )
    # Replay-safe: the save is an upsert by date.
    await ctx.step("save", lambda: agent.save_reflection(reflection), replay_safe=True)
    # The day's Gardener run (its proposals are the day's batch); one per day,
    # keyed by date, so a re-synthesized day does not propose again.
    async def queue_garden() -> str:
        return (await ctx.engine.enqueue("garden", {"date": date}, request_id=f"garden:{date}")).id

    await ctx.step("garden", queue_garden, replay_safe=True)
    return {"date": date}


async def enqueue_day(date: str) -> Job:
    """Queue the synthesis of `date` under the one request id per day shared
    by the CLI, launchd and the API, so a run never races an interrupted one:
    an unfinished job resumes, a failed one is retried from its last
    checkpoint, and a day that already succeeded is synthesized afresh."""
    return await get_engine().enqueue(
        "synthesize", {"date": date}, request_id=f"daily-synthesis:{date}", rerun=True
    )


async def skip_reason(date: str, force: bool = False) -> str | None:
    """Why `date` is not synthesized now: no agent sessions that day in local
    history (commits are not sessions, and wisen's own runs do not count:
    wisen/connectors/claude_code.py), or a reflection already made after the
    day's last session ended. An explicit request (`force`) overrides both."""
    if force:
        return None
    sessions = [s for s in (await ingest_sessions(date)).sessions if s.date == date]
    if not sessions:
        return "no new sessions"
    existing = await get_store().get_reflection(date)
    made = existing.get("synthesized_at") if existing else None
    if made and datetime.fromisoformat(made).astimezone(UTC) >= max(s.ended_at.astimezone(UTC) for s in sessions):
        return f"already synthesized at {made}, no session since"
    return None


async def synthesize_day(date: str, force: bool = False) -> Job | Skipped:
    """Synthesize `date` (see `enqueue_day`) and work the queue until it is
    final, unless `skip_reason` says to leave the day alone."""
    reason = await skip_reason(date, force)
    if reason is not None:
        return Skipped(date, reason)
    engine = get_engine()
    job = await enqueue_day(date)
    return await engine.run_until_done(job.id)


async def garden_day(date: str) -> Job:
    """Run the day's Gardener job (queued by its synthesis) until it is final."""
    engine = get_engine()
    job = await engine.enqueue("garden", {"date": date}, request_id=f"garden:{date}")
    return await engine.run_until_done(job.id)
