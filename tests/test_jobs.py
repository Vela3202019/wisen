"""
Job engine and proposal state machine on a temp SQLite file: a synthesis job
killed (SIGKILL) between the Observer and the Synthesizer resumes without
calling the Observer again and saves one reflection; request ids return the
original job; expired leases are reclaimed and fenced; an interrupted unsafe
step is surfaced, not re-run; retries back off; waits resume on a signal or a
timeout; proposals refuse illegal moves and keep their first decision.
No model or network needed. Run: python3 -m tests.test_jobs
"""
import asyncio
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wisen import store as store_mod
from wisen.ingestion.pipeline import IngestResult
from wisen.jobs import LEASE, JobEngine, LeaseLost
from wisen.proposals import IllegalTransition, Proposals, expire_proposal_job
from wisen.store import SqliteStore
from wisen.synthesis import agent
from wisen.synthesis import job as synthesis_job
from wisen.synthesis.schema import DayObservation, Reflection

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
DATE = "2026-10-19"


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def _store(tmp: str, name: str) -> SqliteStore:
    return SqliteStore(Path(tmp) / f"{name}.db", "local")


def _rows(store: SqliteStore, sql: str, *params) -> list[sqlite3.Row]:
    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()


# ── synthesis killed mid-run ──────────────────────────────────────────────────

OBSERVATION = DayObservation(
    mood_signals_user="short prompts", mood_signals_agent="careful", topics_raw=["hooks"],
    key_moments=[], commit_drift="none", config_changes_observed=[], friction_observed=[],
    yesterday_recs_followed=[], yesterday_recs_ignored=[], model_usage_observed=[], day_summary="a day",
)
MOOD = {"label": "iterating", "summary": "small loops", "evidence": "a quote"}
QUESTION = {"question": "why?", "evidence": "a quote"}
REFLECTION = Reflection(
    date=DATE, mind_map_nodes=[{"topic": "hooks", "weight": 0.8, "is_new": True}], mind_map_edges=[],
    your_mood=MOOD, your_question=QUESTION, agent_mood=MOOD, agent_question=QUESTION,
    alignment_score=0.7, alignment_label="mild-friction", friction_points=[], recommendations=[],
    config_diffs=[], friction_deltas=[], model_usage=[], acted_on_recommendations=[],
    seven_day_pattern=[], synthesis_context="ctx",
)


def _fake_synthesis(calls: Path, crash_on: str | None) -> None:
    """Swap the model calls for canned outputs that log each call to `calls`;
    `crash_on` names the output type whose call kills the process."""
    def log(name: str) -> None:
        with open(calls, "a") as f:
            f.write(name + "\n")

    async def generate(system_prompt, user_prompt, output_type, model):
        log(output_type.__name__)
        if output_type.__name__ == crash_on:
            os.kill(os.getpid(), signal.SIGKILL)
        return OBSERVATION if output_type is DayObservation else REFLECTION.model_copy()

    async def ingest(date):
        return IngestResult()

    save = agent.save_reflection

    async def logged_save(reflection):
        log("save")
        await save(reflection)

    agent._generate = generate
    agent.save_reflection = logged_save
    synthesis_job.ingest_all = ingest
    synthesis_job.get_model = lambda: "test:model"


def _crash_child(calls: str) -> None:
    """Runs in a subprocess (WISEN_DB_PATH points at the test file): start a
    synthesis job and die inside the Synthesizer's model call."""
    _fake_synthesis(Path(calls), crash_on="Reflection")
    engine = JobEngine(store_mod.get_store(), {"synthesize": synthesis_job.run_synthesis_job})

    async def go():
        job = await engine.enqueue("synthesize", {"date": DATE}, request_id="daily")
        await engine.run_until_done(job.id)

    asyncio.run(go())


async def check_kill_and_restart(tmp: str):
    db, calls = Path(tmp) / "crash.db", Path(tmp) / "calls.txt"
    env = {**os.environ, "WISEN_DB_PATH": str(db), "WISEN_USER_ID": "local"}
    child = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "tests.test_jobs", "--crash-child", str(calls)],
        check=False, env=env, cwd=Path(__file__).resolve().parent.parent,
    )
    assert child.returncode == -signal.SIGKILL, child.returncode
    assert calls.read_text().split() == ["DayObservation", "Reflection"], calls.read_text()

    store = SqliteStore(db, "local")
    (job,) = _rows(store, "SELECT id, state FROM jobs")
    assert job["state"] == "leased", job["state"]
    steps = {r["name"]: r["state"] for r in _rows(store, "SELECT name, state FROM job_steps")}
    assert steps == {"observe": "done", "synthesize": "started"}, steps

    # Restart: a new process (fresh worker id) once the dead worker's lease has expired.
    os.environ["WISEN_DB_PATH"] = str(db)
    store_mod._store = None
    _fake_synthesis(calls, crash_on=None)
    engine = JobEngine(store, {"synthesize": synthesis_job.run_synthesis_job},
                       clock=lambda: datetime.now(UTC) + 2 * LEASE)
    done = await engine.run_until_done(job["id"])
    assert done.state == "succeeded" and done.result == {"date": DATE} and done.attempts == 2, done
    # The Observer ran once; the interrupted Synthesizer re-ran; one save, one reflection.
    assert calls.read_text().split() == ["DayObservation", "Reflection", "Reflection", "save"], calls.read_text()
    assert len(_rows(store, "SELECT 1 FROM reflections")) == 1
    # Resending the launch's request id returns the finished job instead of running again.
    again = await engine.enqueue("synthesize", {"date": DATE}, request_id="daily")
    assert again.id == job["id"] and again.state == "succeeded"
    # Once the job is final its step outputs are dropped; names and times stay.
    outputs = _rows(store, "SELECT name, output FROM job_steps ORDER BY started_at")
    assert [r["name"] for r in outputs] == ["observe", "synthesize", "save"], outputs
    assert all(r["output"] is None for r in outputs), [dict(r) for r in outputs]
    print("✓ jobs: SIGKILL between Observer and Synthesizer resumes; Observer not re-run; one save")


# ── engine mechanics ──────────────────────────────────────────────────────────

async def check_request_id(tmp: str):
    store, clock = _store(tmp, "request"), Clock()
    runs = []

    async def noop(ctx, args):
        runs.append(args)
        return {"ran": len(runs)}

    engine = JobEngine(store, {"noop": noop}, clock=clock)
    a = await engine.enqueue("noop", {"x": 1}, request_id="r1")
    b = await engine.enqueue("noop", {"x": 1}, request_id="r1")
    assert a.id == b.id and len(_rows(store, "SELECT 1 FROM jobs")) == 1
    try:
        await engine.enqueue("noop", {"x": 2}, request_id="r1")
        raise AssertionError("a reused request id with other args must be refused")
    except ValueError:
        pass
    assert await engine.run_once() and not await engine.run_once()
    c = await engine.enqueue("noop", {"x": 1}, request_id="r1")
    assert c.id == a.id and c.state == "succeeded" and c.result == {"ran": 1} and len(runs) == 1, c
    print("✓ jobs: a duplicate request id returns the same job and its original result")


async def check_lease_recovery(tmp: str):
    store, clock = _store(tmp, "lease"), Clock()

    async def noop(ctx, args):
        return "ok"

    a = JobEngine(store, {"noop": noop}, clock=clock)
    b = JobEngine(store, {"noop": noop}, clock=clock)
    job = await a.enqueue("noop", {})
    assert (await asyncio.to_thread(a._claim)).id == job.id  # worker A claims, then goes silent
    assert await b.recover() == 0  # lease still valid
    clock.advance(LEASE + timedelta(seconds=1))
    assert await b.recover() == 1 and (await b.get(job.id)).state == "queued"
    assert await b.run_once()
    assert (await b.get(job.id)).state == "succeeded"
    # A wakes up late: its writes are fenced off by the lease it lost.
    try:
        await asyncio.to_thread(a._begin_step, job.id, "late", True)
        raise AssertionError("a worker that lost its lease must not write")
    except LeaseLost:
        pass
    await asyncio.to_thread(a._finish, job, "failed", None, "late")
    assert (await b.get(job.id)).state == "succeeded"

    # A lease that expires on the last attempt fails the job instead of looping.
    last = await a.enqueue("noop", {}, max_attempts=1)
    await asyncio.to_thread(a._claim)
    clock.advance(LEASE + timedelta(seconds=1))
    await b.recover()
    got = await b.get(last.id)
    assert got.state == "failed" and got.error.startswith("LeaseExpired"), got
    print("✓ jobs: an expired lease is reclaimed; the old holder is fenced off")


class _Crash(BaseException):
    """Stands in for the process dying mid-step."""


async def check_replay_policy(tmp: str):
    store, clock = _store(tmp, "replay"), Clock()
    calls: list[str] = []

    def effect(name: str, crash: bool):
        async def run():
            calls.append(name)
            if crash:
                raise _Crash
            return name
        return run

    async def unsafe(ctx, args):
        await ctx.step("read", effect("read", False))
        await ctx.step("push", effect("push", len(calls) < 3), replay_safe=False)

    async def safe(ctx, args):
        await ctx.step("model", effect("model", calls.count("model") == 0))

    engine = JobEngine(store, {"unsafe": unsafe, "safe": safe}, clock=clock)
    job = await engine.enqueue("unsafe", {})
    try:
        await engine.run_once()
        raise AssertionError("expected the simulated crash")
    except _Crash:
        pass
    clock.advance(LEASE + timedelta(seconds=1))
    assert await engine.run_once()
    got = await engine.get(job.id)
    assert got.state == "failed" and got.error.startswith("StepInterrupted"), got
    assert calls == ["read", "push"], calls  # neither re-run

    calls.clear()
    job = await engine.enqueue("safe", {})
    try:
        await engine.run_once()
    except _Crash:
        pass
    clock.advance(LEASE + timedelta(seconds=1))
    assert await engine.run_once()
    assert (await engine.get(job.id)).state == "succeeded" and calls == ["model", "model"], calls

    async def twice(ctx, args):
        await ctx.step("same", effect("a", False))
        await ctx.step("same", effect("b", False))

    engine.handlers["twice"] = twice
    job = await engine.enqueue("twice", {}, max_attempts=1)
    await engine.run_once()
    assert "used twice" in (await engine.get(job.id)).error
    print("✓ jobs: an interrupted unsafe step is surfaced, not re-run; a safe one re-runs")


async def check_retry_and_timers(tmp: str):
    store, clock = _store(tmp, "retry"), Clock()
    failures = [RuntimeError("boom")]

    async def flaky(ctx, args):
        if failures:
            raise failures.pop()
        return "ok"

    async def broken(ctx, args):
        raise RuntimeError("always")

    engine = JobEngine(store, {"flaky": flaky, "broken": broken}, clock=clock)
    job = await engine.enqueue("flaky", {})
    assert await engine.run_once()
    got = await engine.get(job.id)
    assert got.state == "queued" and got.attempts == 1 and got.error == "RuntimeError: boom", got
    assert await engine.next_wake() == T0 + timedelta(seconds=30)
    assert not await engine.run_once()  # backing off
    clock.advance(timedelta(seconds=30))
    assert await engine.run_once() and (await engine.get(job.id)).state == "succeeded"

    job = await engine.enqueue("broken", {}, max_attempts=2)
    await engine.run_once()
    clock.advance(timedelta(seconds=30))
    await engine.run_once()
    got = await engine.get(job.id)
    assert got.state == "failed" and got.attempts == 2, got

    # A timer is a job with a future run_after; the next wake comes from the table.
    later = await engine.enqueue("flaky", {}, run_after=clock.now + timedelta(hours=1))
    assert await engine.next_wake() == clock.now + timedelta(hours=1)
    assert not await engine.run_once()
    clock.advance(timedelta(hours=1))
    assert await engine.run_once() and (await engine.get(later.id)).state == "succeeded"
    print("✓ jobs: errors retry with backoff then fail; timers wake from stored run_after")


async def check_wait_and_signal(tmp: str):
    store, clock = _store(tmp, "wait"), Clock()

    async def approval(ctx, args):
        got = await ctx.wait("decision", args["key"], timeout=timedelta(hours=1))
        return {"got": got}

    engine = JobEngine(store, {"approval": approval}, clock=clock)
    job = await engine.enqueue("approval", {"key": "decision:p1"})
    assert await engine.run_once()
    assert (await engine.get(job.id)).state == "waiting"
    assert await engine.next_wake() == T0 + timedelta(hours=1)
    assert await engine.signal("decision:p1", {"ok": True}) == {"ok": True}
    assert await engine.signal("decision:p1", {"ok": False}) == {"ok": True}  # first write wins
    assert await engine.run_once()
    got = await engine.get(job.id)
    assert got.state == "succeeded" and got.result == {"got": {"ok": True}} and got.attempts == 1, got

    job = await engine.enqueue("approval", {"key": "never"})
    await engine.run_once()
    assert not await engine.run_once()
    clock.advance(timedelta(hours=1))
    assert await engine.run_once()
    assert (await engine.get(job.id)).result == {"got": None}
    print("✓ jobs: a wait resumes on its signal or its timeout")


async def check_run_forever(tmp: str):
    store = _store(tmp, "forever")

    async def noop(ctx, args):
        return "ok"

    engine = JobEngine(store, {"noop": noop})
    worker = asyncio.create_task(engine.run_forever())
    job = await engine.enqueue("noop", {})
    for _ in range(100):
        if (await engine.get(job.id)).state == "succeeded":
            break
        await asyncio.sleep(0.05)
    worker.cancel()
    with suppress(asyncio.CancelledError):
        await worker
    assert (await engine.get(job.id)).state == "succeeded"
    print("✓ jobs: the worker loop picks up a job as soon as it is queued")


# ── proposals ─────────────────────────────────────────────────────────────────

async def check_proposals(tmp: str):
    store, clock = _store(tmp, "proposals"), Clock()
    props = Proposals(store, clock)
    p = await props.create("wisen:CLAUDE.md", {"diff": "+ one rule"}, request_id="c1", actor="gardener",
                           expires_at=T0 + timedelta(days=2))
    assert p.state == "proposed"
    assert (await props.create("wisen:CLAUDE.md", {}, request_id="c1", actor="gardener")).id == p.id
    assert len(_rows(store, "SELECT 1 FROM proposals")) == 1

    for frm, to in [("proposed", "applied"), ("approved", "applied"), ("proposed", "approved")]:
        try:
            await props.transition(p.id, frm, to, request_id=f"bad-{frm}-{to}", actor="test")
            raise AssertionError(f"{frm} -> {to} must be refused")
        except IllegalTransition:
            pass
    assert (await props.get(p.id)).state == "proposed" and len(await props.history(p.id)) == 1

    first = await props.decide(p.id, "approved", request_id="tg-1", actor="telegram")
    second = await props.decide(p.id, "rejected", request_id="web-1", actor="web", note="no")
    resent = await props.decide(p.id, "approved", request_id="tg-1", actor="telegram")
    assert first == second == resent and first.decision == "approved", (first, second)
    assert (await props.get(p.id)).state == "approved"

    applied = await props.transition(p.id, "approved", "applied", request_id="apply-1", actor="laptop")
    assert await props.transition(p.id, "approved", "applied", request_id="apply-1", actor="laptop") == applied
    trail = [(e.from_state, e.to_state, e.actor) for e in await props.history(p.id)]
    assert trail == [(None, "proposed", "gardener"), ("proposed", "approved", "telegram"),
                     ("approved", "applied", "laptop")], trail

    # Expiry: a stored timer job, written with the proposal, expires it.
    engine = JobEngine(store, {"expire-proposal": expire_proposal_job}, clock=clock)
    q = await props.create("wisen:AGENTS.md", {}, request_id="c2", actor="gardener",
                           expires_at=T0 + timedelta(hours=1))
    assert not await engine.run_once()  # not due yet
    clock.advance(timedelta(hours=1))
    try:
        await props.decide(q.id, "approved", request_id="late", actor="web")
        raise AssertionError("a decision past the deadline must be refused")
    except IllegalTransition:
        pass
    assert await engine.run_once()
    assert (await props.get(q.id)).state == "expired"
    assert (await props.history(q.id))[-1].actor == "system"
    assert not await props.expire(q.id)  # a repeated wake changes nothing
    print("✓ proposals: illegal moves refused; first decision kept; request ids replay; expiry timer")


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        await check_kill_and_restart(tmp)
        await check_request_id(tmp)
        await check_lease_recovery(tmp)
        await check_replay_policy(tmp)
        await check_retry_and_timers(tmp)
        await check_wait_and_signal(tmp)
        await check_run_forever(tmp)
        await check_proposals(tmp)
    print("\nall job engine tests passed")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--crash-child"]:
        _crash_child(sys.argv[2])
    else:
        asyncio.run(main())
