"""
The server's mailbox: one SQLite file (WAL) holding users, browser sessions,
paired devices, received packets, proposals and their decisions, push
subscriptions, and the job queue. Every rule about that data lives here; the
HTTP app (server.py) only translates requests.

Each user gets the same job engine and proposal state machine the laptop runs
(wisen/jobs.py, wisen/proposals.py), on this file and scoped to their user id.
The server's own jobs ('local' runner) are push sends, proposal expiry and the
daily schedule. Device jobs ('device' runner) are leased to the user's paired
laptops over HTTPS (`poll`), under the same lease rules, so a laptop that
sleeps mid-job just lets its lease expire and the job waits for it.

Every write is one transaction (lesson 1 in wisen/jobs.py); every row carries
a user id and no query reads across users.
"""
import asyncio
import json
import re
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel

from wisen.cloud import auth
from wisen.cloud.push import Gone, PushSender
from wisen.jobs import Job, JobContext, JobEngine, insert_job, ts
from wisen.proposals import (
    Decision,
    Proposal,
    ProposalEvent,
    Proposals,
    expire_proposal_job,
)
from wisen.store import SqliteStore, migrate

SESSION_TTL = timedelta(days=30)
PAIRING_TTL = timedelta(minutes=10)
SEEN_EVERY = timedelta(minutes=1)  # how often a device's last_seen_at is rewritten
POLL_RECHECK = 2.0  # seconds; a long poll rechecks at least this often (timers come due without a signal)
# What a device may upload into the mailbox. Observation packets are stage B;
# job status arrives only as the end of a device job.
ACCEPTED = ("result", "usage", "proposal")
DEVICE_KINDS = ("synthesize",)
USER_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
DAILY_AT = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

MIGRATIONS = [
    """
    CREATE TABLE users (
        id            TEXT PRIMARY KEY,
        password_hash TEXT NOT NULL,
        timezone      TEXT NOT NULL,
        daily_at      TEXT NOT NULL,
        created_at    TEXT NOT NULL
    );
    CREATE TABLE web_sessions (
        token_hash TEXT PRIMARY KEY,
        user_id    TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    CREATE TABLE pairing_codes (
        code_hash  TEXT PRIMARY KEY,
        user_id    TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        device_id  TEXT
    );
    -- One token per device: re-pairing makes a new device.
    CREATE TABLE devices (
        id           TEXT PRIMARY KEY,
        user_id      TEXT NOT NULL,
        name         TEXT NOT NULL,
        token_hash   TEXT NOT NULL UNIQUE,
        created_at   TEXT NOT NULL,
        last_seen_at TEXT,
        revoked_at   TEXT
    );
    CREATE INDEX devices_user ON devices (user_id, created_at);
    CREATE TABLE push_subscriptions (
        id           TEXT PRIMARY KEY,
        user_id      TEXT NOT NULL,
        endpoint     TEXT NOT NULL,
        subscription TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        UNIQUE (user_id, endpoint)
    );
    -- The latest packet per (type, key): a day's result, a proposal, usage.
    CREATE TABLE packets (
        user_id     TEXT NOT NULL,
        packet_type TEXT NOT NULL,
        key         TEXT NOT NULL,
        packet_id   TEXT NOT NULL,
        device_id   TEXT NOT NULL,
        body        TEXT NOT NULL,
        received_at TEXT NOT NULL,
        PRIMARY KEY (user_id, packet_type, key)
    );
    """,
]


class Conflict(ValueError):
    """The request clashes with what is stored (a reused id, a changed proposal)."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _owner(device_id: str) -> str:
    return f"device:{device_id}"


def fire_time(day: date, timezone: str, daily_at: str) -> datetime:
    """When the daily run for `day` fires: `daily_at` local time, in UTC."""
    hour, minute = map(int, daily_at.split(":"))
    return datetime.combine(day, time(hour, minute), ZoneInfo(timezone)).astimezone(UTC)


class Mailbox:
    def __init__(self, path: Path, *, sender: PushSender | None, clock: Callable[[], datetime] = _utcnow):
        self.path = path
        self.sender = sender
        self.clock = clock
        # Owns no user; used for the tables that are looked up before a user
        # is known (sessions, tokens, pairing codes) and for migrations.
        self._db = SqliteStore(path, "")
        with closing(self._db._connect()) as conn:
            migrate(conn, "cloud", MIGRATIONS)
        self._stores: dict[str, SqliteStore] = {}
        self._engines: dict[str, JobEngine] = {}
        self._changed: dict[str, asyncio.Event] = {}

    # ── per-user engine and proposals ─────────────────────────────────────────

    def store(self, user_id: str) -> SqliteStore:
        if user_id not in self._stores:
            self._stores[user_id] = SqliteStore(self.path, user_id)
        return self._stores[user_id]

    def engine(self, user_id: str) -> JobEngine:
        if user_id not in self._engines:
            self._engines[user_id] = JobEngine(self.store(user_id), {
                "push": self._push_job,
                "expire-proposal": expire_proposal_job,
                "schedule-daily": self._schedule_job,
            }, clock=self.clock)
        return self._engines[user_id]

    def proposals(self, user_id: str) -> Proposals:
        return Proposals(self.store(user_id), self.clock)

    def _changes(self, user_id: str) -> asyncio.Event:
        """Set (and replaced) whenever something a device polls for changes."""
        return self._changed.setdefault(user_id, asyncio.Event())

    def _notify(self, user_id: str) -> None:
        self._changed.pop(user_id, asyncio.Event()).set()

    async def run_forever(self, every: float = 30.0) -> None:
        """Run each user's engine in this process, and keep each user's daily
        schedule in place; picks up users added while running."""
        running: dict[str, asyncio.Task] = {}
        try:
            while True:
                for user_id in await asyncio.to_thread(self.user_ids):
                    await self.ensure_schedule(user_id)
                    if user_id not in running or running[user_id].done():
                        running[user_id] = asyncio.create_task(self.engine(user_id).run_forever())
                await asyncio.sleep(every)
        finally:
            for task in running.values():
                task.cancel()

    # ── users and browser sessions ────────────────────────────────────────────

    def set_user(self, user_id: str, password: str, timezone: str = "UTC", daily_at: str = "07:00") -> bool:
        """Create the user, or set a new password, timezone and daily run time
        for an existing one. True if created."""
        if not USER_ID.match(user_id):
            raise ValueError("a user id is 1-63 lowercase letters, digits, '.', '_' or '-'")
        if not DAILY_AT.match(daily_at):
            raise ValueError("daily_at is HH:MM, 24-hour")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown timezone {timezone!r}") from None
        if len(password) < 10:
            raise ValueError("use a password of at least 10 characters")
        with self._db.transaction() as conn:
            created = conn.execute(
                "INSERT INTO users (id, password_hash, timezone, daily_at, created_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (id) DO NOTHING",
                (user_id, auth.hash_password(password), timezone, daily_at, ts(self.clock())),
            ).rowcount == 1
            if not created:
                conn.execute(
                    "UPDATE users SET password_hash = ?, timezone = ?, daily_at = ? WHERE id = ?",
                    (auth.hash_password(password), timezone, daily_at, user_id),
                )
                # A new password signs every browser out.
                conn.execute("DELETE FROM web_sessions WHERE user_id = ?", (user_id,))
        return created

    def user_ids(self) -> list[str]:
        with closing(self._db._connect()) as conn:
            return [r["id"] for r in conn.execute("SELECT id FROM users ORDER BY id")]

    def _user(self, user_id: str) -> sqlite3.Row | None:
        with closing(self._db._connect()) as conn:
            return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    def check_login(self, user_id: str, password: str) -> bool:
        user = self._user(user_id)
        if user is None:
            auth.hash_password(password)  # spend the same time as a real check
            return False
        return auth.check_password(password, user["password_hash"])

    def new_session(self, user_id: str) -> str:
        token = auth.new_token("wss_")
        now = self.clock()
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (ts(now),))
            conn.execute(
                "INSERT INTO web_sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (auth.token_hash(token), user_id, ts(now), ts(now + SESSION_TTL)),
            )
        return token

    def session_user(self, token: str) -> str | None:
        with closing(self._db._connect()) as conn:
            row = conn.execute(
                "SELECT user_id FROM web_sessions WHERE token_hash = ? AND expires_at > ?",
                (auth.token_hash(token), ts(self.clock())),
            ).fetchone()
        return row["user_id"] if row else None

    def end_session(self, token: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM web_sessions WHERE token_hash = ?", (auth.token_hash(token),))

    # ── devices ───────────────────────────────────────────────────────────────

    def new_pairing_code(self, user_id: str) -> tuple[str, str]:
        """A one-time code for `wisen cloud pair`, and when it expires."""
        code, now = auth.new_code(), self.clock()
        expires = ts(now + PAIRING_TTL)
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM pairing_codes WHERE expires_at <= ?", (ts(now),))
            conn.execute(
                "INSERT INTO pairing_codes (code_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (auth.token_hash(auth.normalize_code(code)), user_id, ts(now), expires),
            )
        return code, expires

    def pair(self, code: str, name: str) -> dict | None:
        """Spend a pairing code on a new device and return its token, which is
        shown this once; only its hash is kept. None if the code is unknown,
        used or expired."""
        token, device_id, now = auth.new_token("wsd_"), uuid.uuid4().hex, ts(self.clock())
        with self._db.transaction() as conn:
            row = conn.execute(
                "UPDATE pairing_codes SET device_id = ? WHERE code_hash = ? AND device_id IS NULL "
                "AND expires_at > ? RETURNING user_id",
                (device_id, auth.token_hash(auth.normalize_code(code)), now),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "INSERT INTO devices (id, user_id, name, token_hash, created_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (device_id, row["user_id"], name, auth.token_hash(token), now, now),
            )
        return {"user_id": row["user_id"], "device_id": device_id, "token": token}

    def device_for_token(self, token: str) -> tuple[str, str] | None:
        """(user id, device id) for a live device token; None once revoked."""
        now = self.clock()
        with closing(self._db._connect()) as conn:
            row = conn.execute(
                "SELECT id, user_id, last_seen_at FROM devices WHERE token_hash = ? AND revoked_at IS NULL",
                (auth.token_hash(token),),
            ).fetchone()
        if row is None:
            return None
        if row["last_seen_at"] is None or datetime.fromisoformat(row["last_seen_at"]) < now - SEEN_EVERY:
            with self._db.transaction() as conn:
                conn.execute("UPDATE devices SET last_seen_at = ? WHERE id = ?", (ts(now), row["id"]))
        return row["user_id"], row["id"]

    def devices(self, user_id: str) -> list[dict]:
        with closing(self._db._connect()) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id, name, created_at, last_seen_at, revoked_at FROM devices WHERE user_id = ? "
                "ORDER BY created_at DESC",
                (user_id,),
            )]

    def revoke_device(self, user_id: str, device_id: str) -> bool:
        """Revoke a device: its token stops working at once (401), and any job
        it holds goes back to the queue instead of waiting out the lease."""
        now = ts(self.clock())
        with self._db.transaction() as conn:
            revoked = conn.execute(
                "UPDATE devices SET revoked_at = ? WHERE user_id = ? AND id = ? AND revoked_at IS NULL",
                (now, user_id, device_id),
            ).rowcount == 1
            if revoked:
                conn.execute(
                    "UPDATE jobs SET state = 'queued', run_after = ?, lease_owner = NULL, lease_expires_at = NULL, "
                    "updated_at = ? WHERE user_id = ? AND state = 'leased' AND lease_owner = ?",
                    (now, now, user_id, _owner(device_id)),
                )
        return revoked

    # ── packets ───────────────────────────────────────────────────────────────

    def store_packet(self, user_id: str, device_id: str, packet: BaseModel) -> bool:
        """Keep a checked packet (wisen.packets.receive) from a user's device.
        False if the mailbox already holds this exact packet. A new or changed
        day's result, or a new proposal, queues a push in the same transaction."""
        if packet.user_id != user_id or packet.device_id != device_id:
            raise Conflict("packet user_id and device_id must match the device token")
        key = {"result": lambda: packet.date, "usage": lambda: "latest",
               "proposal": lambda: packet.proposal_id}[packet.packet_type]()
        now = self.clock()
        with self.store(user_id).transaction() as conn:
            row = conn.execute(
                "SELECT packet_id FROM packets WHERE user_id = ? AND packet_type = ? AND key = ?",
                (user_id, packet.packet_type, key),
            ).fetchone()
            if row and row["packet_id"] == packet.packet_id:
                return False
            if row and packet.packet_type == "proposal":
                raise Conflict(f"proposal {key} was already sent with different content; a proposal never changes")
            conn.execute(
                "INSERT INTO packets (user_id, packet_type, key, packet_id, device_id, body, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (user_id, packet_type, key) DO UPDATE SET "
                "packet_id = excluded.packet_id, device_id = excluded.device_id, body = excluded.body, "
                "received_at = excluded.received_at",
                (user_id, packet.packet_type, key, packet.packet_id, device_id, packet.model_dump_json(), ts(now)),
            )
            if packet.packet_type == "result":
                self._queue_push(conn, user_id, f"result:{packet.packet_id}", now, title="Reflection ready",
                                 body=f"{packet.date} · {packet.alignment_label}", url=f"/#day-{packet.date}")
            elif packet.packet_type == "proposal":
                self._record_proposal(conn, user_id, device_id, packet)
                self._queue_push(conn, user_id, f"proposal:{packet.proposal_id}", now,
                                 title="Proposal waiting", body=packet.target, url=f"/#proposal-{packet.proposal_id}")
        if packet.packet_type in ("result", "proposal"):
            self.engine(user_id).wake()
        return True

    def _record_proposal(self, conn: sqlite3.Connection, user_id: str, device_id: str, packet: BaseModel) -> None:
        payload = packet.model_dump(include={"diff", "rationale", "evidence", "eval_summary"})
        expires_at = datetime.fromisoformat(packet.expires_at) if packet.expires_at else None
        if expires_at is not None and expires_at.tzinfo is None:
            raise Conflict("expires_at needs a UTC offset")
        try:
            self.proposals(user_id).create_in(
                conn, packet.target, payload, f"packet:{packet.packet_id}", _owner(device_id), expires_at,
                packet.proposal_id,
            )
        except ValueError as e:
            raise Conflict(str(e)) from None

    def _queue_push(self, conn: sqlite3.Connection, user_id: str, key: str, now: datetime, **message: str) -> None:
        insert_job(conn, user_id, "push", message, request_id=f"push:{key}", run_after=now, max_attempts=5, now=now)

    def packets(self, user_id: str, packet_type: str, limit: int = 400) -> list[dict]:
        """Stored packets of one type, newest key first (a result's key is its date)."""
        with closing(self._db._connect()) as conn:
            return [json.loads(r["body"]) for r in conn.execute(
                "SELECT body FROM packets WHERE user_id = ? AND packet_type = ? ORDER BY key DESC LIMIT ?",
                (user_id, packet_type, limit),
            )]

    # ── proposals and decisions ───────────────────────────────────────────────

    async def decide(
        self, user_id: str, proposal_id: str, decision: str, *, request_id: str, actor: str, note: str | None,
    ) -> Decision:
        """The human's approve or reject. The first decision wins (a retry or a
        second device gets it back), and polling devices hear about it at once."""
        result = await self.proposals(user_id).decide(
            proposal_id, decision, request_id=request_id, actor=actor, note=note
        )
        self._notify(user_id)
        return result

    async def inbox(self, user_id: str, limit: int = 50) -> list[Proposal]:
        return await self.proposals(user_id).list(limit=limit)

    # ── device jobs ───────────────────────────────────────────────────────────

    async def enqueue_device_job(self, user_id: str, kind: str, args: dict, *, request_id: str, rerun: bool) -> Job:
        if kind not in DEVICE_KINDS:
            raise ValueError(f"unknown device job kind {kind!r}")
        job = await self.engine(user_id).enqueue(kind, args, request_id=request_id, rerun=rerun, runner="device")
        self._notify(user_id)
        return job

    def device_jobs(self, user_id: str, states: tuple[str, ...]) -> list[Job]:
        with closing(self._db._connect()) as conn:
            return [Job.from_row(r) for r in conn.execute(
                f"SELECT * FROM jobs WHERE user_id = ? AND runner = 'device' "
                f"AND state IN ({', '.join('?' * len(states))}) ORDER BY run_after",
                (user_id, *states),
            )]

    async def poll(
        self, user_id: str, device_id: str, cursor: int, wait: float,
    ) -> tuple[Job | None, list[tuple[int, ProposalEvent]]]:
        """Long poll for one device: wait up to `wait` seconds for a device job
        to lease or for decisions after event id `cursor`, and return both."""
        engine, proposals = self.engine(user_id), self.proposals(user_id)
        deadline = asyncio.get_running_loop().time() + wait
        while True:
            changed = self._changes(user_id)  # taken before checking, so no change is missed
            await engine.recover()
            job = await engine.claim_as(_owner(device_id), "device")
            events = await proposals.events_since(cursor)
            remaining = deadline - asyncio.get_running_loop().time()
            if job or events or remaining <= 0:
                return job, events
            try:
                await asyncio.wait_for(changed.wait(), min(remaining, POLL_RECHECK))
            except TimeoutError:
                pass

    async def heartbeat(self, user_id: str, device_id: str, job_id: str) -> bool:
        return await self.engine(user_id).heartbeat_as(job_id, _owner(device_id))

    async def finish(self, user_id: str, device_id: str, job_id: str, status: BaseModel) -> bool:
        """End a device job with the device's job status packet. False if the
        device no longer holds the job (its lease expired and moved on)."""
        if status.user_id != user_id or status.device_id != device_id or status.job_id != job_id:
            raise Conflict("job status must name this job, user and device")
        engine = self.engine(user_id)
        job = await engine.get(job_id)
        if job is None or job.runner != "device" or job.kind != status.kind:
            raise Conflict("no such device job")
        if status.state == "succeeded":
            return await engine.finish_as(job_id, _owner(device_id), status.model_dump(mode="json"))
        return await engine.fail_as(job_id, _owner(device_id), f"DeviceJobFailed: {status.error_class or 'unknown'}")

    # ── the daily schedule ────────────────────────────────────────────────────

    async def ensure_schedule(self, user_id: str) -> None:
        """Make sure the user's next daily run is a stored timer. Safe to call
        on every start: the request id makes it a no-op once it exists."""
        user = await asyncio.to_thread(self._user, user_id)
        now = self.clock()
        today = now.astimezone(ZoneInfo(user["timezone"])).date()
        day = today if now < fire_time(today, user["timezone"], user["daily_at"]) else today + timedelta(days=1)
        await asyncio.to_thread(self._schedule, user_id, day, None)
        self.engine(user_id).wake()

    def _schedule(self, user_id: str, day: date, synthesize: str | None) -> None:
        user = self._user(user_id)
        now = self.clock()
        with self.store(user_id).transaction() as conn:
            if synthesize is not None:
                # Same request id as the laptop's own daily run and the
                # dashboard trigger: a day is one job however it was asked for.
                insert_job(conn, user_id, "synthesize", {"date": synthesize}, request_id=f"daily-synthesis:{synthesize}",
                           run_after=now, max_attempts=3, now=now, runner="device")
            insert_job(conn, user_id, "schedule-daily", {"date": day.isoformat()},
                       request_id=f"schedule-daily:{day.isoformat()}",
                       run_after=fire_time(day, user["timezone"], user["daily_at"]), max_attempts=3, now=now)

    async def _schedule_job(self, ctx: JobContext, args: dict) -> dict:
        """The daily timer: ask a device to synthesize yesterday, and set the
        timer for tomorrow, in one transaction. Wakes may repeat (lesson 5):
        both writes are keyed by request id, so a second run changes nothing."""
        user_id = ctx.engine.store.user_id
        day = date.fromisoformat(args["date"])
        yesterday = (day - timedelta(days=1)).isoformat()
        await ctx.step("enqueue", lambda: asyncio.to_thread(
            self._schedule, user_id, day + timedelta(days=1), yesterday), replay_safe=True)
        self._notify(user_id)
        return {"synthesize": yesterday}

    # ── push ──────────────────────────────────────────────────────────────────

    def add_push_subscription(self, user_id: str, subscription: dict) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO push_subscriptions (id, user_id, endpoint, subscription, created_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (user_id, endpoint) DO UPDATE SET "
                "subscription = excluded.subscription",
                (uuid.uuid4().hex, user_id, subscription["endpoint"], json.dumps(subscription), ts(self.clock())),
            )

    def remove_push_subscription(self, user_id: str, endpoint: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM push_subscriptions WHERE user_id = ? AND endpoint = ?", (user_id, endpoint))

    def _subscriptions(self, user_id: str) -> list[dict[str, Any]]:
        with closing(self._db._connect()) as conn:
            return [{"id": r["id"], "subscription": json.loads(r["subscription"])} for r in conn.execute(
                "SELECT id, subscription FROM push_subscriptions WHERE user_id = ? ORDER BY created_at", (user_id,)
            )]

    async def _push_job(self, ctx: JobContext, args: dict) -> dict:
        """Send one notification to each of the user's subscribed browsers.
        A send is a side effect, so each is its own step that is not safe to
        re-run (lesson 3): a crash mid-send fails the job for a human to check
        instead of notifying twice, and a retry skips sends already done."""
        if self.sender is None:
            return {"sent": 0, "push": "off"}
        user_id = ctx.engine.store.user_id
        subs = await ctx.step("subscriptions", lambda: asyncio.to_thread(self._subscriptions, user_id),
                              replay_safe=True, output=list[dict[str, Any]])
        payload = json.dumps(args)
        outcomes = []
        for sub in subs:
            async def send(sub=sub) -> str:
                try:
                    await self.sender.send(sub["subscription"], payload)
                except Gone:
                    await asyncio.to_thread(self._drop_subscription, user_id, sub["id"])
                    return "gone"
                return "sent"
            outcomes.append(await ctx.step(f"send:{sub['id']}", send, replay_safe=False, output=str))
        return {"sent": outcomes.count("sent"), "gone": outcomes.count("gone")}

    def _drop_subscription(self, user_id: str, subscription_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM push_subscriptions WHERE user_id = ? AND id = ?", (user_id, subscription_id))
