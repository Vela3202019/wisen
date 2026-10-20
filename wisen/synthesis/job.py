"""
Synthesis as a durable job: one day's ingest + Observer + Synthesizer + save,
checkpointed after each stage by the job engine (wisen/jobs.py). If the
process dies after the Observer, the rerun starts at the Synthesizer.
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
    return {"date": date}


def daily_request_id(date: str) -> str:
    """The one request id per day, shared by the CLI, launchd and the API, so
    a rerun resumes that day's job instead of starting a second one."""
    return f"daily-synthesis:{date}"


async def synthesize_day(date: str) -> Job:
    """Queue (or resume) the synthesis job for `date` and work the queue
    until it is final. A day that already succeeded returns its job; one that
    failed is retried from its last checkpoint."""
    engine = get_engine()
    job = await engine.enqueue("synthesize", {"date": date}, request_id=daily_request_id(date))
    return await engine.run_until_done(job.id)
