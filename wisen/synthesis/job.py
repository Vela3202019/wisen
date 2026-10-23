"""
Synthesis as a durable job: one day's ingest + Observer + Synthesizer + save,
checkpointed after each stage by the job engine (wisen/jobs.py). If the
process dies after the Observer, the rerun starts at the Synthesizer. A saved
day queues that day's Gardener run (wisen/gardener/loop.py).
"""
from wisen.ingestion.pipeline import ingest_all
from wisen.jobs import Job, JobContext, get_engine
from wisen.llm import get_model
from wisen.synthesis import agent
from wisen.synthesis.schema import DayObservation, Reflection


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


async def synthesize_day(date: str) -> Job:
    """Synthesize `date` (see `enqueue_day`) and work the queue until it is final."""
    engine = get_engine()
    job = await enqueue_day(date)
    return await engine.run_until_done(job.id)


async def garden_day(date: str) -> Job:
    """Run the day's Gardener job (queued by its synthesis) until it is final."""
    engine = get_engine()
    job = await engine.enqueue("garden", {"date": date}, request_id=f"garden:{date}")
    return await engine.run_until_done(job.id)
