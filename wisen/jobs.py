"""
Durable job engine, built from scratch on the local SQLite store.

A job is a row in `jobs`. Its handler is an async function that does its work
in named steps (`ctx.step`). Each finished step's output is saved, so a job
that reruns after a crash skips the steps it already finished and carries on
from the last checkpoint.

The design follows the lessons from Earendil's Pi Durable
(https://earendil.com/posts/pi-durable/ and
https://earendil.com/posts/pi-durable-on-cloudflare/), cited as "lesson N":
  1. Only committed state exists: every change is one transaction.
  2. Everything is a task with a checkpoint after each phase.
  3. Every step declares whether it is safe to re-run after a crash.
  4. Request ids make retries harmless: the receipt is written with the work.
  5. Timers are recomputed from stored tasks on every start; wakes are
     at-least-once, so handlers are idempotent.
  6. Crash-safe decisions are first-write-wins memos.

Job states:
  queued     runnable once `run_after` passes
  leased     a worker holds it until `lease_expires_at`; heartbeats extend it
  waiting    parked in `ctx.wait` until its signal arrives or its timeout passes
  succeeded  final; `result` holds the handler's return value
  failed     `error` says why (attempts used up, or an unsafe step was
             interrupted); sending its request id again re-queues it

The full design is docs/plan/cloud-architecture.md §6.2.
"""
import asyncio
import json
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import TypeAdapter

from wisen.store import SqliteStore, get_store

LEASE = timedelta(seconds=60)
RETRY_BASE = timedelta(seconds=30)  # doubles per attempt
RETRY_CAP = timedelta(hours=1)
MAX_IDLE = 60.0  # seconds; an idle worker rechecks the table at least this often,
                 # since another process (the CLI, launchd) may have enqueued
TERMINAL = ("succeeded", "failed")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def ts(value: datetime) -> str:
    """Fixed-width UTC timestamp, so stored times compare correctly as text."""
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class LeaseLost(Exception):
    """This worker no longer holds the job's lease; another worker may."""


class StepInterrupted(Exception):
    """A step that is not safe to re-run started but never finished."""


class _Parked(BaseException):
    """Control flow: `ctx.wait` parked the job. A BaseException, so a handler's
    `except Exception` cannot swallow it."""


@dataclass
class Job:
    id: str
    kind: str
    args: dict
    state: str
    attempts: int
    max_attempts: int
    run_after: str | None
    request_id: str | None
    result: Any
    error: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        return cls(
            id=row["id"],
            kind=row["kind"],
            args=json.loads(row["args"]),
            state=row["state"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            run_after=row["run_after"],
            request_id=row["request_id"],
            result=json.loads(row["result"]) if row["result"] is not None else None,
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


Handler = Callable[["JobContext", dict], Awaitable[Any]]


def insert_job(
    conn: sqlite3.Connection,
    user_id: str,
    kind: str,
    args: dict,
    *,
    request_id: str | None,
    run_after: datetime,
    max_attempts: int,
    now: datetime,
) -> sqlite3.Row:
    """Insert a queued job inside the caller's transaction, so it commits with
    whatever else that transaction writes (lesson 1). The job row is the
    receipt for its request id (lesson 4): a resent request returns the job
    already recorded for it, re-queued if it had failed."""
    args_json = json.dumps(args, sort_keys=True)
    if request_id is not None:
        row = conn.execute(
            "SELECT * FROM jobs WHERE user_id = ? AND request_id = ?", (user_id, request_id)
        ).fetchone()
        if row:
            if row["kind"] != kind or row["args"] != args_json:
                raise ValueError(f"request id {request_id!r} was already used for a different job")
            if row["state"] != "failed":
                return row
            # Sending the request again retries a failed job in place: its
            # finished steps are kept, so it resumes from its last checkpoint.
            return conn.execute(
                "UPDATE jobs SET state = 'queued', attempts = 0, run_after = ?, error = NULL, "
                "updated_at = ? WHERE id = ? RETURNING *",
                (ts(now), ts(now), row["id"]),
            ).fetchone()
    job_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO jobs (id, user_id, kind, args, state, attempts, max_attempts, run_after, "
        "request_id, created_at, updated_at) VALUES (?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?)",
        (job_id, user_id, kind, args_json, max_attempts, ts(run_after), request_id, ts(now), ts(now)),
    )
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


class JobContext:
    """What a handler gets: its job, plus journaled `step` and `wait`."""

    def __init__(self, engine: "JobEngine", job: Job):
        self.engine = engine
        self.job = job
        self._names: set[str] = set()

    def _use_name(self, name: str) -> None:
        # A step is found again on replay by its name alone, so names must be
        # deterministic and unique within one job (lesson 2).
        if name in self._names:
            raise ValueError(f"step name {name!r} used twice in one {self.job.kind} job")
        self._names.add(name)

    async def step(
        self,
        name: str,
        fn: Callable[[], Awaitable[Any]],
        *,
        replay_safe: bool,
        output: Any = Any,
    ) -> Any:
        """Run `fn` once and checkpoint its result (lesson 2). If this job
        already finished the step on an earlier run, return the saved result
        without calling `fn`.

        `output` is the result's type, used to save it as JSON and load it
        back. Every step declares `replay_safe`; `replay_safe=False` marks a step whose effect must not happen
        twice (create a branch, send a push): if a crash interrupts it, the
        job fails with `StepInterrupted` for a human to check instead of
        running it again (lesson 3). A step that raises is treated as not
        having happened, so raise only when its effect did not take place."""
        self._use_name(name)
        adapter = TypeAdapter(output)
        saved = await asyncio.to_thread(self.engine._begin_step, self.job.id, name, replay_safe)
        if saved is not None:
            return adapter.validate_json(saved)
        try:
            value = await fn()
        except Exception:
            await asyncio.to_thread(self.engine._abandon_step, self.job.id, name)
            raise
        dumped = adapter.dump_json(value).decode()
        await asyncio.to_thread(self.engine._end_step, self.job.id, name, dumped)
        # Hand back the saved form, so a first run and a replay see the same value.
        return adapter.validate_json(dumped)

    async def wait(self, name: str, key: str, *, timeout: timedelta | None = None) -> Any:
        """Park the job until `engine.signal(key, payload)`, then return the
        payload; return None if `timeout` passes first. Journaled like a step,
        so a resumed job gets the same answer. The worker is free while the
        job waits; parking ends this run and the job is claimed again later."""
        self._use_name(name)
        saved = await asyncio.to_thread(self.engine._wait, self.job.id, name, key, timeout)
        return json.loads(saved)


class JobEngine:
    """The queue and its worker. Every method commits before it returns; the
    engine keeps no state in memory that a restart would lose (lesson 1)."""

    def __init__(
        self,
        store: SqliteStore,
        handlers: dict[str, Handler],
        *,
        clock: Callable[[], datetime] = _utcnow,
    ):
        self.store = store
        self.handlers = handlers
        self.clock = clock
        # New per process, so a restarted process never mistakes a lease its
        # previous life took for its own.
        self.worker_id = uuid.uuid4().hex
        self._wakeup = asyncio.Event()

    @property
    def _uid(self) -> str:
        return self.store.user_id

    # ── commands ──────────────────────────────────────────────────────────────

    def _enqueue(self, kind, args, request_id, run_after, max_attempts) -> Job:
        now = self.clock()
        with self.store.transaction() as conn:
            row = insert_job(
                conn, self._uid, kind, args, request_id=request_id,
                run_after=run_after or now, max_attempts=max_attempts, now=now,
            )
        return Job.from_row(row)

    async def enqueue(
        self,
        kind: str,
        args: dict,
        *,
        request_id: str | None = None,
        run_after: datetime | None = None,
        max_attempts: int = 3,
    ) -> Job:
        """Queue a job, runnable from `run_after` (default: now). With a
        `request_id`, sending the same request again returns the same job."""
        job = await asyncio.to_thread(self._enqueue, kind, args, request_id, run_after, max_attempts)
        self._wakeup.set()
        return job

    def _get(self, job_id: str) -> Job | None:
        with closing(self.store._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE user_id = ? AND id = ?", (self._uid, job_id)
            ).fetchone()
        return Job.from_row(row) if row else None

    async def get(self, job_id: str) -> Job | None:
        return await asyncio.to_thread(self._get, job_id)

    def _signal(self, key: str, payload: Any) -> Any:
        now = ts(self.clock())
        with self.store.transaction() as conn:
            # First write wins (lesson 6): a second signal for the same key
            # changes nothing and gets the first payload back.
            conn.execute(
                "INSERT INTO job_signals (user_id, key, payload, at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (user_id, key) DO NOTHING",
                (self._uid, key, json.dumps(payload), now),
            )
            conn.execute(
                "UPDATE jobs SET run_after = ?, updated_at = ? "
                "WHERE user_id = ? AND state = 'waiting' AND wait_key = ?",
                (now, now, self._uid, key),
            )
            stored = conn.execute(
                "SELECT payload FROM job_signals WHERE user_id = ? AND key = ?", (self._uid, key)
            ).fetchone()["payload"]
        return json.loads(stored)

    async def signal(self, key: str, payload: Any) -> Any:
        """Deliver an event to whatever job waits (or will wait) on `key`, and
        return the payload stored for it."""
        stored = await asyncio.to_thread(self._signal, key, payload)
        self._wakeup.set()
        return stored

    # ── the worker ────────────────────────────────────────────────────────────

    def _recover(self) -> int:
        """Put jobs whose lease expired back in the queue: their worker crashed
        or the laptop slept. A lease that expires on the job's last attempt
        fails the job, so a job that keeps killing its process stops."""
        now = ts(self.clock())
        with self.store.transaction() as conn:
            failed = conn.execute(
                "UPDATE jobs SET state = 'failed', error = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, updated_at = ? WHERE user_id = ? AND state = 'leased' "
                "AND lease_expires_at <= ? AND attempts >= max_attempts",
                ("LeaseExpired: the last attempt never finished", now, self._uid, now),
            ).rowcount
            requeued = conn.execute(
                "UPDATE jobs SET state = 'queued', run_after = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, updated_at = ? WHERE user_id = ? AND state = 'leased' "
                "AND lease_expires_at <= ?",
                (now, now, self._uid, now),
            ).rowcount
        return failed + requeued

    async def recover(self) -> int:
        return await asyncio.to_thread(self._recover)

    def _claim(self) -> Job | None:
        now = self.clock()
        with self.store.transaction() as conn:
            # One statement picks and leases the job, so two workers can never
            # claim the same one. Resuming from a wait is not a new attempt.
            row = conn.execute(
                "UPDATE jobs SET state = 'leased', "
                "attempts = attempts + CASE WHEN state = 'waiting' THEN 0 ELSE 1 END, "
                "lease_owner = ?, lease_expires_at = ?, wait_key = NULL, updated_at = ? "
                "WHERE id = (SELECT id FROM jobs WHERE user_id = ? AND state IN ('queued', 'waiting') "
                "AND run_after <= ? ORDER BY run_after, created_at LIMIT 1) RETURNING *",
                (self.worker_id, ts(now + LEASE), ts(now), self._uid, ts(now)),
            ).fetchone()
        return Job.from_row(row) if row else None

    def _next_wake(self) -> datetime | None:
        with closing(self.store._connect()) as conn:
            row = conn.execute(
                "SELECT MIN(t) FROM ("
                "SELECT run_after AS t FROM jobs WHERE user_id = ? AND state IN ('queued', 'waiting') "
                "UNION ALL "
                "SELECT lease_expires_at FROM jobs WHERE user_id = ? AND state = 'leased')",
                (self._uid, self._uid),
            ).fetchone()
        return datetime.fromisoformat(row[0]) if row[0] else None

    async def next_wake(self) -> datetime | None:
        """When the earliest stored timer, retry or lease expiry comes due."""
        return await asyncio.to_thread(self._next_wake)

    async def run_once(self) -> bool:
        """Recover expired leases, then claim and run one due job. False when
        nothing was due."""
        await self.recover()
        job = await asyncio.to_thread(self._claim)
        if job is None:
            return False
        await self._execute(job)
        return True

    async def run_forever(self) -> None:
        """The worker loop. No timer lives in memory: each pass recomputes the
        next wake from the stored jobs, so a restart loses nothing (lesson 5)."""
        while True:
            self._wakeup.clear()
            try:
                ran = await self.run_once()
            except sqlite3.OperationalError as e:  # e.g. the file stayed locked past its timeout
                print(f"[jobs] {e}; retrying")
                ran = False
            if not ran:
                await self._sleep_until_next_wake()

    async def run_until_done(self, job_id: str) -> Job:
        """Work the queue until `job_id` is final; for one-shot callers
        (`wisen synthesize`, the launchd run)."""
        while True:
            self._wakeup.clear()
            job = await self.get(job_id)
            if job.state in TERMINAL:
                return job
            if not await self.run_once():
                await self._sleep_until_next_wake()

    async def _sleep_until_next_wake(self) -> None:
        wake = await self.next_wake()
        delay = MAX_IDLE if wake is None else (wake - self.clock()).total_seconds()
        try:
            await asyncio.wait_for(self._wakeup.wait(), min(max(delay, 0.0), MAX_IDLE))
        except TimeoutError:
            pass

    async def _execute(self, job: Job) -> None:
        handler = self.handlers.get(job.kind)
        if handler is None:
            await asyncio.to_thread(self._finish, job, "failed", None, f"no handler for job kind {job.kind!r}")
            return
        heartbeat = asyncio.create_task(self._heartbeat(job.id))
        try:
            result = await handler(JobContext(self, job), job.args)
        except _Parked:
            return
        except LeaseLost:
            return  # another worker owns the job now and decides what happens next
        except StepInterrupted as e:
            await asyncio.to_thread(self._finish, job, "failed", None, _describe(e))
        except Exception as e:  # noqa: BLE001 - any handler error is retried, then recorded
            await asyncio.to_thread(self._retry_or_fail, job, _describe(e))
        else:
            await asyncio.to_thread(self._finish, job, "succeeded", result, None)
        finally:
            heartbeat.cancel()

    async def _heartbeat(self, job_id: str) -> None:
        while True:
            await asyncio.sleep(LEASE.total_seconds() / 3)
            if not await asyncio.to_thread(self._extend_lease, job_id):
                return

    def _extend_lease(self, job_id: str) -> bool:
        now = self.clock()
        with self.store.transaction() as conn:
            return conn.execute(
                "UPDATE jobs SET lease_expires_at = ? WHERE id = ? AND state = 'leased' AND lease_owner = ?",
                (ts(now + LEASE), job_id, self.worker_id),
            ).rowcount == 1

    def _finish(self, job: Job, state: str, result: Any, error: str | None) -> None:
        now = ts(self.clock())
        with self.store.transaction() as conn:
            # Fenced on the lease: a worker whose lease was taken over cannot
            # overwrite the new owner's outcome.
            done = conn.execute(
                "UPDATE jobs SET state = ?, result = ?, error = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, updated_at = ? WHERE id = ? AND state = 'leased' AND lease_owner = ?",
                (state, json.dumps(result), error, now, job.id, self.worker_id),
            ).rowcount
            if done and state == "succeeded":
                _prune_outputs(conn, job.id)

    def _retry_or_fail(self, job: Job, error: str) -> None:
        if job.attempts >= job.max_attempts:
            self._finish(job, "failed", None, error)
            return
        now = self.clock()
        delay = min(RETRY_BASE * 2 ** (job.attempts - 1), RETRY_CAP)
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE jobs SET state = 'queued', run_after = ?, error = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, updated_at = ? WHERE id = ? AND state = 'leased' AND lease_owner = ?",
                (ts(now + delay), error, ts(now), job.id, self.worker_id),
            )

    # ── steps (called by JobContext) ──────────────────────────────────────────

    def _check_lease(self, conn: sqlite3.Connection, job_id: str) -> None:
        if conn.execute(
            "SELECT 1 FROM jobs WHERE id = ? AND state = 'leased' AND lease_owner = ?",
            (job_id, self.worker_id),
        ).fetchone() is None:
            raise LeaseLost(job_id)

    def _begin_step(self, job_id: str, name: str, replay_safe: bool) -> str | None:
        """The saved output if the step already finished, else None after
        marking it started. The committed `started` mark is how a later run
        knows a crash interrupted the step."""
        with self.store.transaction() as conn:
            self._check_lease(conn, job_id)
            row = conn.execute(
                "SELECT state, output FROM job_steps WHERE job_id = ? AND name = ?", (job_id, name)
            ).fetchone()
            if row and row["state"] == "done":
                return row["output"]
            if row and not replay_safe:
                raise StepInterrupted(
                    f"step {name!r} started but never finished and is not safe to re-run; "
                    "check whether it took effect"
                )
            conn.execute(
                "INSERT INTO job_steps (job_id, name, state, replay_safe, started_at) "
                "VALUES (?, ?, 'started', ?, ?) ON CONFLICT (job_id, name) DO NOTHING",
                (job_id, name, int(replay_safe), ts(self.clock())),
            )
        return None

    def _end_step(self, job_id: str, name: str, output: str) -> None:
        with self.store.transaction() as conn:
            self._check_lease(conn, job_id)
            conn.execute(
                "UPDATE job_steps SET state = 'done', output = ?, finished_at = ? WHERE job_id = ? AND name = ?",
                (output, ts(self.clock()), job_id, name),
            )

    def _abandon_step(self, job_id: str, name: str) -> None:
        with self.store.transaction() as conn:
            self._check_lease(conn, job_id)
            conn.execute("DELETE FROM job_steps WHERE job_id = ? AND name = ?", (job_id, name))

    def _wait(self, job_id: str, name: str, key: str, timeout: timedelta | None) -> str:
        now = self.clock()
        with self.store.transaction() as conn:
            self._check_lease(conn, job_id)
            row = conn.execute(
                "SELECT state, output, started_at FROM job_steps WHERE job_id = ? AND name = ?",
                (job_id, name),
            ).fetchone()
            if row and row["state"] == "done":
                return row["output"]
            if row is None:
                conn.execute(
                    "INSERT INTO job_steps (job_id, name, state, replay_safe, started_at) "
                    "VALUES (?, ?, 'started', 1, ?)",
                    (job_id, name, ts(now)),
                )
            started = datetime.fromisoformat(row["started_at"]) if row else now
            deadline = started + timeout if timeout is not None else None
            signal = conn.execute(
                "SELECT payload FROM job_signals WHERE user_id = ? AND key = ?", (self._uid, key)
            ).fetchone()
            if signal or (deadline is not None and now >= deadline):
                output = signal["payload"] if signal else "null"
                conn.execute(
                    "UPDATE job_steps SET state = 'done', output = ?, finished_at = ? WHERE job_id = ? AND name = ?",
                    (output, ts(now), job_id, name),
                )
                return output
            # The timeout is a stored timer: run_after is the deadline.
            conn.execute(
                "UPDATE jobs SET state = 'waiting', wait_key = ?, run_after = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, updated_at = ? WHERE id = ?",
                (key, ts(deadline) if deadline else None, ts(now), job_id),
            )
        raise _Parked


def _prune_outputs(conn: sqlite3.Connection, job_id: str) -> None:
    # Step outputs only matter while a job can still resume, and a failed job
    # resumes when its request is sent again. Drop them once it succeeds, so
    # derived day data does not linger in the journal; the step names and
    # times stay as the record of what ran.
    conn.execute("UPDATE job_steps SET output = NULL WHERE job_id = ?", (job_id,))


def _describe(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"


_engine: JobEngine | None = None


def get_engine() -> JobEngine:
    global _engine
    if _engine is None:
        # Imported here: the handlers' modules import this one.
        from wisen.proposals import expire_proposal_job
        from wisen.synthesis.job import run_synthesis_job

        _engine = JobEngine(get_store(), {
            "synthesize": run_synthesis_job,
            "expire-proposal": expire_proposal_job,
        })
    return _engine
