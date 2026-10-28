"""
REM on the server (stage B, docs/plan/cloud-architecture.md §3.2): the
Synthesizer and the Gardener run here, over packets only, so the morning
reflection is made while every laptop sleeps. The laptops' Observers (NREM)
read the raw history and send their notes as observation packets; the
Gardener drafts against the files the laptops uploaded as config snapshots.
Applying an approved change stays on the laptop that sent the file.

  synthesize       server-synthesis:<date>   merge every laptop's notes on the
                                             day, synthesize, run the day's
                                             Gardener, then publish the
                                             reflection (one push, with the batch)
  gardener-resume  gardener-resume:<run>     once every proposal of a run is
                                             decided: resume it with the decisions

Synthesis runs on `claude -p` on the owner's subscription by default
(WISEN_SYNTH_MODEL, set by `wisen cloud serve` in stage B;
wisen/synthesis/claude_cli.py), or on the API model with WISEN_SYNTH_MODEL=api.
The Gardener needs the API model (WISEN_MODEL and its key, in the service's
environment file): it uses tools, which `claude -p` does not serve. Chat runs
on DeepSeek (WISEN_CHAT_MODEL; wisen/chat/agent.py).

Imported only when a stage-B job runs, so a stage-A server loads no model code.
"""
import asyncio
import hashlib
import json
from typing import get_origin

from wisen.gardener import loop
from wisen.jobs import JobContext, ts
from wisen.llm import get_model
from wisen.store import SqliteStore
from wisen.synthesis import agent
from wisen.synthesis.schema import DayObservation, Reflection


def merge_observations(notes: list[tuple[str, dict]]) -> DayObservation:
    """One DayObservation from each laptop's notes on the same day, so the
    day gets one reflection: each text field keeps every laptop's text under
    its device name, and the lists are joined (repeats dropped)."""
    if len(notes) == 1:
        return DayObservation.model_validate(notes[0][1])
    merged: dict = {}
    for name, field in DayObservation.model_fields.items():
        if get_origin(field.annotation) is list:
            items = [item for _, note in notes for item in note[name]]
            merged[name] = [item for i, item in enumerate(items) if item not in items[:i]]
        else:
            merged[name] = "\n\n".join(f"[{device}] {note[name]}" for device, note in notes if note[name])
    return DayObservation.model_validate(merged)


async def _reflect(observation: DayObservation, yesterday: dict | None, past: list[dict], day: str) -> Reflection:
    try:
        return await agent.reflect(observation, yesterday, past, day, get_model())
    except SystemExit as e:   # the CLI adapter's "claude not installed": fail the job, not the server
        raise RuntimeError(str(e)) from None


async def _history(store: SqliteStore, day: str) -> tuple[dict | None, list[dict]]:
    """Yesterday's reflection and the past 7 days, from the server's own record."""
    past = await store.list_reflections(limit=7, before=day)
    return (past[0] if past else None), past


async def synthesize_job(mailbox, ctx: JobContext, args: dict) -> dict:
    store, day = ctx.engine.store, args["date"]
    user = store.user_id
    reason = await ctx.step("check", lambda: asyncio.to_thread(
        mailbox.skip_reason, user, day, args.get("force", False)), replay_safe=True, output=str | None)
    if reason is not None:
        print(f"[server] synthesize {day} skipped: {reason}")
        return {"date": day, "skipped": reason}

    # Merged afresh on every run, so a retry or a rerun takes in notes sent
    # since the last one; the steps that use them are named by those notes.
    # Notes sent while this run works make it go round again.
    merged, garden = None, {}
    while (key := _key(notes := await asyncio.to_thread(mailbox.observations, user, day))) != merged:
        merged = key
        garden = await _reflect_on(mailbox, ctx, day, notes, key)
    return {"date": day, **garden}


def _key(notes: list[tuple[str, dict, str]]) -> str:
    return hashlib.sha256(json.dumps([(device, at) for device, _, at in notes]).encode()).hexdigest()[:16]


async def _reflect_on(mailbox, ctx: JobContext, day: str, notes: list[tuple[str, dict, str]], key: str) -> dict:
    store = ctx.engine.store
    user = store.user_id
    observation = merge_observations([(device, note) for device, note, _ in notes])
    yesterday, past = await _history(store, day)
    reflection = await ctx.step(f"synthesize:{key}", lambda: _reflect(observation, yesterday, past, day),
                                replay_safe=True, output=Reflection)
    # Saved before the Gardener runs, which reads it; published after it, so
    # the one push names the day's batch. Both are upserts by date. Stamped
    # with the newest note it reflects on, which `skip_reason` compares with.
    await ctx.step(f"save:{key}", lambda: store.upsert_reflection(
        {**reflection.model_dump(), "synthesized_at": max(at for _, _, at in notes)}), replay_safe=True)
    garden = await ctx.step(f"garden:{key}", lambda: garden_day(mailbox, user, day), replay_safe=True, output=dict)
    await ctx.step(f"publish:{key}", lambda: asyncio.to_thread(mailbox.publish_result, user, day), replay_safe=True)
    return garden


def _ran(store: SqliteStore, day: str) -> bool:
    return bool(loop._rows(store, "SELECT 1 FROM gardener_runs WHERE user_id = ? AND date = ?", day))


async def garden_day(mailbox, user: str, day: str) -> dict:
    """The day's Gardener run, once per day (a re-synthesized day does not
    propose again). A failure is reported, never fatal: the reflection still
    goes out."""
    store = mailbox.store(user)
    if await asyncio.to_thread(_ran, store, day):
        return {"proposals": 0}
    reflection = await store.get_reflection(day)
    if reflection is None or not loop.wants_tending(reflection):
        return {"proposals": 0}
    if len(await loop.recent_reflections(store, day)) < 2:
        return {"proposals": 0, "gardener": "only one reflection so far"}
    try:
        run = await loop.propose(store, day, await asyncio.to_thread(mailbox.snapshot, user))
        ids = await asyncio.to_thread(loop.record_run, store, mailbox.proposals(user), run, {}, day,
                                      loop.NOT_REPLAYED)
    except Exception as e:  # noqa: BLE001 - a model or network failure must not hold back the reflection
        print(f"[server] gardener failed for {day}: {type(e).__name__}: {e}")
        return {"proposals": 0, "gardener": f"failed: {type(e).__name__}"}
    return {"proposals": len(ids)}


async def resume_job(mailbox, ctx: JobContext, args: dict) -> dict:
    store, run_id = ctx.engine.store, args["run_id"]
    state = loop._rows(store, "SELECT state FROM gardener_runs WHERE user_id = ? AND id = ?", run_id)
    if not state or state[0]["state"] != "paused":
        return {"run_id": run_id, "resumed": False}

    async def resume() -> dict:
        return await loop.resume(store, run_id, await asyncio.to_thread(mailbox.snapshot, store.user_id))

    resumed = await ctx.step("resume", resume, replay_safe=True, output=dict)
    await ctx.step("save", lambda: asyncio.to_thread(
        loop._save_resumed, store, run_id, resumed, ts(ctx.engine.clock())), replay_safe=True)
    return {"run_id": run_id, "resumed": True}
