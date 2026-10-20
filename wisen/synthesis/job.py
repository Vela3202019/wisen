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

    observation = await ctx.step("observe", observe_day, output=DayObservation)
    reflection = await ctx.step(
        "synthesize", lambda: agent.reflect(observation, yesterday_doc, past_docs, date, model), output=Reflection,
    )
    # Replay-safe: the save is an upsert by date.
    await ctx.step("save", lambda: agent.save_reflection(reflection))
    return {"date": date}


async def synthesize_day(date: str, request_id: str | None = None) -> Job:
    """Queue a synthesis job for `date` and work the queue until it is final.
    Sending the same `request_id` again returns that same job."""
    engine = get_engine()
    job = await engine.enqueue("synthesize", {"date": date}, request_id=request_id)
    return await engine.run_until_done(job.id)
