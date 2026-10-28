"""
The server's mailbox: one SQLite file (WAL) holding users, browser sessions,
paired devices, received packets, proposals and their decisions, push
subscriptions, linked Telegram chats, and the job queue. Every rule about that
data lives here; the HTTP app (server.py) and the Telegram bot (telegram.py)
only translate requests.

Each user gets the same job engine and proposal state machine the laptop runs
(wisen/jobs.py, wisen/proposals.py), on this file and scoped to their user id.
The server's own jobs ('local' runner) are push and Telegram sends, proposal
expiry and the daily schedule. Device jobs ('device' runner) are leased to the
user's paired laptops over HTTPS (`poll`), under the same lease rules, so a
laptop that sleeps mid-job just lets its lease expire and the job waits for it.

Stage B (`stage="b"`, WISEN_CLOUD_STAGE=b; docs/plan/cloud-architecture.md
§3.2): laptops also upload what the REM side needs, and the server runs it
(wisen/cloud/rem.py): their Observer's notes per day (observation packets,
from every laptop) and the current text of the files the Gardener may edit
(config snapshot packets, secret-redacted). The daily schedule then asks the
laptops for fresh notes (an 'observe' device job) and synthesizes the day here
once one answers, or after OBSERVE_GRACE from what is already uploaded, so the
morning reflection is made while every laptop sleeps. Applying an approved
change stays a laptop's job.

Received packets are mirrored into the tables the laptop's agents read
(reflections, daily_metrics, proposal_measures), so the Synthesizer, the
Gardener and chat run here on the same code.

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
from wisen.cloud.telegram import TelegramApi, proposal_message
from wisen.gardener.applier import MEASURE_AFTER
from wisen.gardener.targets import HOME, SnapshotFiles
from wisen.jobs import Job, JobContext, JobEngine, insert_job, ts
from wisen.packets import ResultPacket, result_packet
from wisen.proposals import (
    Decision,
    IllegalTransition,
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
# What a device may upload into the mailbox; stage B adds what the server's
# REM side reads. Job status arrives only as the end of a device job.
ACCEPTED = ("result", "usage", "proposal", "proposal_update", "metrics", "answer")
STAGE_B = ("observation", "config_snapshot")
# ask and search answer a question asked in Telegram (wisen/cloud/telegram.py).
DEVICE_KINDS = ("synthesize", "measure", "publish", "observe", "ask", "search")
STAGES = ("a", "b")
# Stage B: how long the day's synthesis waits for a laptop to send fresh
# notes before it runs on what is already uploaded (a sleeping laptop never
# answers).
OBSERVE_GRACE = timedelta(minutes=30)
SERVER = "server"   # the device id on what the server makes itself (its reflections)
ENVELOPE = ("packet_type", "schema_version", "user_id", "device_id", "packet_id", "created_at")
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
    # 2: stage B reads results, metrics and measurements through the tables
    # the laptop's agents use (wisen/store.py); copy in what stage A received.
    """
    INSERT OR IGNORE INTO reflections (user_id, date, doc)
        SELECT user_id, key, json_set(json_remove(body, '$.packet_type', '$.schema_version', '$.user_id',
                                                  '$.device_id', '$.packet_id', '$.created_at'),
                                      '$.synthesized_at', received_at)
        FROM packets WHERE packet_type = 'result';
    INSERT OR IGNORE INTO daily_metrics (user_id, date, doc)
        SELECT user_id, key, json_remove(body, '$.packet_type', '$.schema_version', '$.user_id', '$.device_id',
                                         '$.packet_id', '$.created_at')
        FROM packets WHERE packet_type = 'metrics';
    INSERT OR IGNORE INTO proposal_measures (proposal_id, user_id, verdict, reason, result, at)
        SELECT json_extract(body, '$.proposal_id'), user_id, json_extract(body, '$.measure.verdict'),
               coalesce(json_extract(body, '$.note'), ''), json_extract(body, '$.measure'), json_extract(body, '$.at')
        FROM packets WHERE packet_type = 'proposal_update' AND json_extract(body, '$.measure') IS NOT NULL;
    """,
    # 3: Telegram links (wisen/cloud/telegram.py).
    """
    -- One linked Telegram chat per user; only it may decide or ask.
    CREATE TABLE telegram_links (
        user_id   TEXT PRIMARY KEY,
        chat_id   INTEGER NOT NULL UNIQUE,
        linked_at TEXT NOT NULL
    );
    """,
]


class Conflict(ValueError):
    """The request clashes with what is stored (a reused id, a changed proposal)."""


class NotPublishable(ValueError):
    """The proposal has no applied branch to publish, or its file has no remote."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _owner(device_id: str) -> str:
    return f"device:{device_id}"


def fire_time(day: date, timezone: str, daily_at: str) -> datetime:
    """When the daily run for `day` fires: `daily_at` local time, in UTC."""
    hour, minute = map(int, daily_at.split(":"))
    return datetime.combine(day, time(hour, minute), ZoneInfo(timezone)).astimezone(UTC)


class Mailbox:
    def __init__(self, path: Path, *, sender: PushSender | None, telegram: TelegramApi | None = None,
                 public_url: str = "", clock: Callable[[], datetime] = _utcnow, stage: str = "a"):
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}; use one of {', '.join(STAGES)}")
        self.path = path
        self.sender = sender
        self.telegram = telegram
        self.public_url = public_url  # for links in Telegram messages
        self.clock = clock
        self.stage = stage
        self.accepted = ACCEPTED + (STAGE_B if stage == "b" else ())
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
                "expire-proposal": self._expire_job,
                "telegram": self._telegram_job,
                "schedule-daily": self._schedule_job,
                "synthesize": self._rem("synthesize_job"),
                "gardener-resume": self._rem("resume_job"),
            }, clock=self.clock)
        return self._engines[user_id]

    def proposals(self, user_id: str) -> Proposals:
        return Proposals(self.store(user_id), self.clock)

    def _rem(self, name: str):
        """A stage-B job handler (wisen/cloud/rem.py), imported on first use
        so a stage-A server never loads model code."""
        async def handler(ctx: JobContext, args: dict) -> Any:
            from wisen.cloud import rem

            return await getattr(rem, name)(self, ctx, args)
        return handler

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

    def device_online(self, user_id: str, within: timedelta) -> bool:
        """Whether one of the user's live devices was heard from in `within`."""
        with closing(self._db._connect()) as conn:
            return conn.execute(
                "SELECT 1 FROM devices WHERE user_id = ? AND revoked_at IS NULL AND last_seen_at > ?",
                (user_id, ts(self.clock() - within)),
            ).fetchone() is not None

    # ── Telegram links ────────────────────────────────────────────────────────

    def link_telegram(self, code: str, chat_id: int) -> str | None:
        """Spend a pairing code (the dashboard's one-time code) on linking a
        Telegram chat to its user, replacing the user's earlier chat. Returns
        the user id; None if the code is unknown, used or expired."""
        now = ts(self.clock())
        with self._db.transaction() as conn:
            row = conn.execute(
                "UPDATE pairing_codes SET device_id = ? WHERE code_hash = ? AND device_id IS NULL "
                "AND expires_at > ? RETURNING user_id",
                (f"telegram:{chat_id}", auth.token_hash(auth.normalize_code(code)), now),
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM telegram_links WHERE chat_id = ?", (chat_id,))
            conn.execute(
                "INSERT INTO telegram_links (user_id, chat_id, linked_at) VALUES (?, ?, ?) "
                "ON CONFLICT (user_id) DO UPDATE SET chat_id = excluded.chat_id, linked_at = excluded.linked_at",
                (row["user_id"], chat_id, now),
            )
        return row["user_id"]

    def telegram_user(self, chat_id: int) -> str | None:
        with closing(self._db._connect()) as conn:
            row = conn.execute("SELECT user_id FROM telegram_links WHERE chat_id = ?", (chat_id,)).fetchone()
        return row["user_id"] if row else None

    def _telegram_chat(self, user_id: str) -> int | None:
        with closing(self._db._connect()) as conn:
            row = conn.execute("SELECT chat_id FROM telegram_links WHERE user_id = ?", (user_id,)).fetchone()
        return row["chat_id"] if row else None

    def unlink_telegram(self, user_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM telegram_links WHERE user_id = ?", (user_id,))

    # ── packets ───────────────────────────────────────────────────────────────

    def store_packet(self, user_id: str, device_id: str, packet: BaseModel) -> bool:
        """Keep a checked packet (wisen.packets.receive) from a user's device.
        False if the mailbox already holds this exact packet. A new or changed
        day's result, or a new proposal, queues a push in the same transaction;
        the caller wakes the user's engine on the event loop."""
        if packet.user_id != user_id or packet.device_id != device_id:
            raise Conflict("packet user_id and device_id must match the device token")
        return self._store_packet(user_id, device_id, packet)

    def _store_packet(self, user_id: str, device_id: str, packet: BaseModel) -> bool:
        key = {"result": lambda: packet.date, "usage": lambda: "latest", "proposal": lambda: packet.proposal_id,
               "proposal_update": lambda: packet.request_id, "metrics": lambda: packet.date,
               # one per laptop: several laptops' notes on a day merge into one reflection
               "observation": lambda: f"{packet.date}:{device_id}",
               "config_snapshot": lambda: f"{device_id}:{packet.target}",
               "answer": lambda: packet.job_id}[packet.packet_type]()
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
            if row and packet.packet_type == "answer":
                return False
            if packet.packet_type == "answer" and not conn.execute(
                "SELECT 1 FROM jobs WHERE user_id = ? AND id = ? AND runner = 'device' AND kind IN ('ask', 'search')",
                (user_id, key),
            ).fetchone():
                raise Conflict(f"no question {key} to answer")
            conn.execute(
                "INSERT INTO packets (user_id, packet_type, key, packet_id, device_id, body, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (user_id, packet_type, key) DO UPDATE SET "
                "packet_id = excluded.packet_id, device_id = excluded.device_id, body = excluded.body, "
                "received_at = excluded.received_at",
                (user_id, packet.packet_type, key, packet.packet_id, device_id, packet.model_dump_json(), ts(now)),
            )
            if packet.packet_type == "result":
                if device_id != SERVER:   # the server's own reflection is already saved, as it made it
                    self._mirror(conn, "reflections", user_id, packet.date, {
                        **packet.model_dump(mode="json", exclude=set(ENVELOPE)), "synthesized_at": ts(now)})
                # The morning briefing carries the day's Gardener batch, which
                # the laptop (or, in stage B, this server) makes first.
                waiting = self._batch_waiting(conn, user_id, packet.date)
                body = f"{packet.date} · {packet.alignment_label}" + (
                    f" · {waiting} proposal{'s' if waiting != 1 else ''} waiting" if waiting else "")
                self._queue_push(conn, user_id, f"result:{packet.packet_id}", now, title="Reflection ready",
                                 body=body, url=f"/#day-{packet.date}")
                self._queue_telegram(conn, user_id, f"result:{packet.packet_id}", now,
                                     text=f"Reflection ready\n{body}", url=f"/#day-{packet.date}")
                for (proposal_id,) in conn.execute(
                    "SELECT id FROM proposals WHERE user_id = ? AND state = 'proposed' "
                    "AND json_extract(payload, '$.batch') = ? ORDER BY created_at", (user_id, packet.date)):
                    self._queue_telegram(conn, user_id, f"proposal:{proposal_id}", now, proposal_id=proposal_id)
            elif packet.packet_type == "proposal":
                self._record_proposal(conn, user_id, device_id, packet)
                if packet.batch is None:  # e.g. a revert after a worse measurement
                    self._queue_push(conn, user_id, f"proposal:{packet.proposal_id}", now, title="Proposal waiting",
                                     body=packet.target, url=f"/#proposal-{packet.proposal_id}")
                elif conn.execute("SELECT 1 FROM packets WHERE user_id = ? AND packet_type = 'result' AND key = ?",
                                  (user_id, packet.batch)).fetchone():
                    # The briefing already went out: one push for the rest of the batch.
                    self._queue_push(conn, user_id, f"batch:{packet.batch}", now, title="Proposals waiting",
                                     body=f"from {packet.batch}", url=f"/#day-{packet.batch}")
                else:
                    return True  # Telegram sends it after the day's briefing, like the push
                # Each proposal is its own Telegram message: it carries its buttons.
                self._queue_telegram(conn, user_id, f"proposal:{packet.proposal_id}", now,
                                     proposal_id=packet.proposal_id)
            elif packet.packet_type == "proposal_update":
                self._record_update(conn, user_id, device_id, packet, now)
            elif packet.packet_type == "metrics":
                self._mirror(conn, "daily_metrics", user_id, packet.date,
                             packet.model_dump(mode="json", exclude=set(ENVELOPE)))
            elif packet.packet_type == "observation":
                self._late_observation(conn, user_id, packet.date, now)
            elif packet.packet_type == "answer":
                self._queue_telegram(conn, user_id, f"answer:{packet.job_id}", now, text=packet.answer)
        return True

    def _mirror(self, conn: sqlite3.Connection, table: str, user_id: str, date: str, doc: dict) -> None:
        conn.execute(f"INSERT INTO {table} (user_id, date, doc) VALUES (?, ?, ?) "
                     f"ON CONFLICT (user_id, date) DO UPDATE SET doc = excluded.doc", (user_id, date, json.dumps(doc)))

    def _late_observation(self, conn: sqlite3.Connection, user_id: str, day: str, now: datetime) -> None:
        """Notes on a day the server already synthesized (a laptop that slept
        through the morning run sends them when it wakes): run the day again.
        The run skips itself if its reflection is already newer than every
        note. Notes on a day not scheduled yet wait for its run."""
        row = conn.execute("SELECT * FROM jobs WHERE user_id = ? AND request_id = ? AND state IN ('succeeded', 'failed')",
                           (user_id, f"server-synthesis:{day}")).fetchone()
        if row is not None:
            insert_job(conn, user_id, row["kind"], json.loads(row["args"]), request_id=row["request_id"],
                       run_after=now, max_attempts=row["max_attempts"], now=now, rerun=True)

    def _batch_waiting(self, conn: sqlite3.Connection, user_id: str, batch: str) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM proposals WHERE user_id = ? AND state = 'proposed' "
            "AND json_extract(payload, '$.batch') = ?", (user_id, batch)).fetchone()[0]

    def _record_update(self, conn: sqlite3.Connection, user_id: str, device_id: str, packet: BaseModel,
                       now: datetime) -> None:
        """A git state the laptop decided. A merge sets the measure timer: a
        device job, due 2 weeks after the merge."""
        try:
            self.proposals(user_id).transition_in(conn, packet.proposal_id, packet.from_state, packet.to_state,
                                                  packet.request_id, _owner(device_id), packet.note)
        except (IllegalTransition, ValueError) as e:
            raise Conflict(str(e)) from None
        if packet.measure is not None:
            # What the server's Gardener reads back (past_proposals).
            conn.execute(
                "INSERT INTO proposal_measures (proposal_id, user_id, verdict, reason, result, at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (proposal_id) DO NOTHING",
                (packet.proposal_id, user_id, packet.measure.verdict, packet.note or "",
                 packet.measure.model_dump_json(), packet.at))
        if packet.to_state == "merged":
            at = datetime.fromisoformat(packet.at)
            insert_job(conn, user_id, "measure", {"proposal_id": packet.proposal_id},
                       request_id=f"measure:{packet.proposal_id}", run_after=at + MEASURE_AFTER, max_attempts=3,
                       now=now, runner="device")
        elif packet.to_state in ("failed", "kept", "revert_proposed"):
            title = {"failed": "Proposal not applied", "kept": "Change kept",
                     "revert_proposed": "Change made things worse"}[packet.to_state]
            self._queue_push(conn, user_id, f"update:{packet.request_id}", now, title=title,
                             body=packet.note or packet.to_state, url=f"/#proposal-{packet.proposal_id}")
            self._queue_telegram(conn, user_id, f"update:{packet.request_id}", now,
                                 text=f"{title}\n{packet.note or packet.to_state}",
                                 url=f"/#proposal-{packet.proposal_id}")

    def _record_proposal(self, conn: sqlite3.Connection, user_id: str, device_id: str, packet: BaseModel) -> None:
        payload = packet.model_dump(include={"diff", "rationale", "evidence", "eval_summary", "batch"})
        expires_at = datetime.fromisoformat(packet.expires_at) if packet.expires_at else None
        if expires_at is not None and expires_at.tzinfo is None:
            raise Conflict("expires_at needs a UTC offset")
        proposals, decided = self.proposals(user_id), packet.decided
        try:
            # Decided while unpaired: the laptop sends what became of it with
            # the proposal, so no expiry here races that decision.
            proposals.create_in(
                conn, packet.target, payload, f"packet:{packet.packet_id}", _owner(device_id), expires_at,
                packet.proposal_id, expiry_timer=decided is None,
            )
            if decided is None:
                return
            at = datetime.fromisoformat(decided.at)
            if decided.state == "expired":
                if expires_at is None or ts(at) < ts(expires_at):
                    raise Conflict(f"proposal {packet.proposal_id} is not past its expiry")
                proposals.transition_in(conn, packet.proposal_id, "proposed", "expired", decided.request_id,
                                        _owner(device_id))
            else:
                # As of when it was made, so one made before the deadline counts.
                proposals.decide_in(conn, packet.proposal_id, decided.state, decided.request_id,
                                    _owner(device_id), decided.note, at)
        except (IllegalTransition, ValueError) as e:
            raise Conflict(str(e)) from None

    def _queue_push(self, conn: sqlite3.Connection, user_id: str, key: str, now: datetime, **message: str) -> None:
        insert_job(conn, user_id, "push", message, request_id=f"push:{key}", run_after=now, max_attempts=5, now=now)

    def _queue_telegram(self, conn: sqlite3.Connection, user_id: str, key: str, now: datetime, **message: str) -> None:
        """One Telegram message (text with an optional dashboard link, or a
        proposal by id), if the bot runs and the user linked a chat. Keyed, so
        a proposal is announced once however many briefings name it."""
        if self.telegram is None or not conn.execute(
                "SELECT 1 FROM telegram_links WHERE user_id = ?", (user_id,)).fetchone():
            return
        insert_job(conn, user_id, "telegram", message, request_id=f"telegram:{key}", run_after=now, max_attempts=5,
                   now=now)

    def packets(self, user_id: str, packet_type: str, limit: int = 400) -> list[dict]:
        """Stored packets of one type, newest key first (a result's key is its date)."""
        with closing(self._db._connect()) as conn:
            return [json.loads(r["body"]) for r in conn.execute(
                "SELECT body FROM packets WHERE user_id = ? AND packet_type = ? ORDER BY key DESC LIMIT ?",
                (user_id, packet_type, limit),
            )]

    # ── stage B: what the server's REM side reads ─────────────────────────────

    def observations(self, user_id: str, day: str) -> list[tuple[str, dict, str]]:
        """Each laptop's latest notes on `day`, as (device name, observation,
        received_at), oldest laptop first."""
        with closing(self._db._connect()) as conn:
            return [(r["name"] or r["device_id"], json.loads(r["body"]), r["received_at"]) for r in conn.execute(
                "SELECT p.device_id, p.body, p.received_at, d.name FROM packets p "
                "LEFT JOIN devices d ON d.id = p.device_id "
                "WHERE p.user_id = ? AND p.packet_type = 'observation' AND p.key LIKE ? "
                "ORDER BY d.created_at, p.device_id",
                (user_id, f"{day}:%"),
            )]

    def snapshot(self, user_id: str) -> SnapshotFiles:
        """The files the Gardener may edit, from every laptop's config
        snapshots: per target, the newest upload of a file that still exists."""
        files: dict[str, dict] = {}
        with closing(self._db._connect()) as conn:
            for r in conn.execute(
                "SELECT device_id, body FROM packets WHERE user_id = ? AND packet_type = 'config_snapshot' "
                "ORDER BY received_at DESC, device_id", (user_id,),
            ):
                body = json.loads(r["body"])
                if body["text"] is not None and body["target"] not in files:
                    files[body["target"]] = {"text": body["text"], "base_blob": body["base_blob"],
                                             "device_id": r["device_id"]}
        return SnapshotFiles(files)

    def skip_reason(self, user_id: str, day: str, force: bool = False) -> str | None:
        """Why the server does not synthesize `day` now (§5.5, read from
        packets instead of local history): no laptop sent notes on it, or its
        reflection is newer than every note. `force` (the owner's Trigger)
        overrides the second only: there is nothing to reflect on without notes."""
        notes = self.observations(user_id, day)
        if not notes:
            return "no observations uploaded"
        if force:
            return None
        with closing(self._db._connect()) as conn:
            row = conn.execute("SELECT json_extract(doc, '$.synthesized_at') AS at FROM reflections "
                               "WHERE user_id = ? AND date = ?", (user_id, day)).fetchone()
        made = row["at"] if row else None
        if made and made >= max(at for _, _, at in notes):
            return f"already synthesized at {made}, no observation since"
        return None

    def publish_result(self, user_id: str, day: str) -> bool:
        """The server's own reflection of `day` (stage B), as a result packet:
        the dashboard and the push read it like one a laptop sent."""
        doc = self.store(user_id)._get_reflection(day)
        packet = ResultPacket.model_validate(result_packet(doc, user=user_id))
        return self._store_packet(user_id, SERVER, packet)

    def timeline(self, user_id: str) -> dict:
        """The hill (cloud-architecture §5.4): each day's metrics, oldest
        first, and a marker per merged Gardener change with its verdict."""
        days = sorted(self.packets(user_id, "metrics"), key=lambda d: d["date"])
        updates = self.packets(user_id, "proposal_update", 5000)
        verdicts = {u["proposal_id"]: u["measure"]["verdict"] for u in updates if u.get("measure")}
        with closing(self._db._connect()) as conn:
            proposals = {r["id"]: r for r in conn.execute(
                "SELECT id, target, state FROM proposals WHERE user_id = ?", (user_id,))}
        changes = sorted(({"proposal_id": u["proposal_id"], "merged_at": u["at"],
                           "target": proposals[u["proposal_id"]]["target"],
                           "state": proposals[u["proposal_id"]]["state"],
                           "verdict": verdicts.get(u["proposal_id"])}
                          for u in updates if u["to_state"] == "merged" and u["proposal_id"] in proposals),
                         key=lambda c: c["merged_at"])
        return {"days": days, "changes": changes}

    # ── proposals and decisions ───────────────────────────────────────────────

    async def decide(
        self, user_id: str, proposal_id: str, decision: str, *, request_id: str, actor: str, note: str | None,
    ) -> Decision:
        """The human's approve or reject. The first decision wins (a retry or a
        second device gets it back), and polling devices hear about it at once."""
        result = await self.proposals(user_id).decide(
            proposal_id, decision, request_id=request_id, actor=actor, note=note
        )
        await asyncio.to_thread(self._after_decision, user_id, proposal_id)
        self._notify(user_id)
        return result

    def _after_decision(self, user_id: str, proposal_id: str) -> None:
        """A proposal the server's Gardener made: once none of its run is left
        undecided, resume the run here with the decisions (§5.2). Proposals a
        laptop made resume on that laptop."""
        store = self.store(user_id)
        with store.transaction() as conn:
            row = conn.execute("SELECT json_extract(payload, '$.run_id') AS run_id FROM proposals "
                               "WHERE user_id = ? AND id = ?", (user_id, proposal_id)).fetchone()
            run_id = row["run_id"] if row else None
            if run_id is None or conn.execute(
                "SELECT 1 FROM proposals WHERE user_id = ? AND state = 'proposed' "
                "AND json_extract(payload, '$.run_id') = ?", (user_id, run_id)).fetchone():
                return
            now = self.clock()
            insert_job(conn, user_id, "gardener-resume", {"run_id": run_id}, request_id=f"gardener-resume:{run_id}",
                       run_after=now, max_attempts=3, now=now)
        self.engine(user_id).wake()

    async def _expire_job(self, ctx: JobContext, args: dict) -> dict:
        result = await expire_proposal_job(ctx, args)
        if result["expired"]:
            await asyncio.to_thread(self._after_decision, ctx.engine.store.user_id, args["proposal_id"])
        return result

    def drafted_here(self, user_id: str, proposal_ids: list[str]) -> dict[str, dict]:
        """Of `proposal_ids`, those the server's Gardener drafted, with what
        the laptop that applies one needs to record it (it has no copy)."""
        if not proposal_ids:
            return {}
        with closing(self._db._connect()) as conn:
            rows = conn.execute(
                f"SELECT id, target, payload FROM proposals WHERE user_id = ? AND id IN "
                f"({', '.join('?' * len(proposal_ids))}) AND json_extract(payload, '$.run_id') IS NOT NULL",
                (user_id, *proposal_ids)).fetchall()
        return {r["id"]: {"target": r["target"], **{k: v for k, v in json.loads(r["payload"]).items() if k in (
            "diff", "rationale", "evidence", "batch", "base_blob", "device_id")}} for r in rows}

    def previous_reflection(self, user_id: str, day: str) -> dict | None:
        """The latest reflection before `day`: the baseline a laptop's
        Observer compares the day against."""
        docs = self.store(user_id)._list_reflections(1, day)
        return docs[0] if docs else None

    async def inbox(self, user_id: str, limit: int = 50) -> list[Proposal]:
        return await self.proposals(user_id).list(limit=limit)

    def inbox_details(self, user_id: str) -> dict[str, dict]:
        """Per proposal, for the dashboard's change log: the decision (with
        its note and who made it) and the latest outcome after it (failed
        with its reason, merged, kept, …)."""
        details: dict[str, dict] = {}
        with closing(self._db._connect()) as conn:
            rows = conn.execute("SELECT proposal_id, to_state, note, at, actor FROM proposal_events "
                                "WHERE user_id = ? ORDER BY id", (user_id,))
            for r in rows:
                d = details.setdefault(r["proposal_id"], {"decision": None, "outcome": None})
                if r["to_state"] in ("approved", "rejected"):
                    d["decision"] = {"decision": r["to_state"], "note": r["note"], "at": r["at"], "actor": r["actor"]}
                elif r["to_state"] != "proposed":
                    d["outcome"] = {"state": r["to_state"], "note": r["note"], "at": r["at"]}
        return details

    async def publish(self, user_id: str, proposal_id: str) -> Job:
        """The owner's "Push & create PR": one device job per proposal
        (request id publish:<id>) for the laptop, which alone can push. A
        second tap while it is queued or running changes nothing; a tap after
        a failure runs it again; once a pull request is recorded, nothing
        runs again. The laptop re-checks the branch before pushing."""
        p = await self.proposals(user_id).get(proposal_id)
        if p is None:
            raise LookupError(f"no proposal {proposal_id}")
        if p.target.partition(":")[0] == HOME:
            raise NotPublishable(f"{p.target} is a file outside any repo: its branch has no remote; "
                                 "merge it on the laptop")
        if p.state != "applied":
            raise NotPublishable(f"proposal {proposal_id} is {p.state}; only an applied branch can be published")
        # One attempt: the failures are the owner's to fix (no remote, no gh
        # login), and the card shows them at once; tapping again retries.
        return await self.enqueue_device_job(user_id, "publish", {"proposal_id": proposal_id},
                                             request_id=f"publish:{proposal_id}", rerun=p.pr_url is None,
                                             max_attempts=1)

    def publish_jobs(self, user_id: str) -> dict[str, dict]:
        """How each proposal's publish job stands, by proposal id: the
        dashboard shows "publishing", the link, or why it failed (an error
        class; the laptop sends no message)."""
        with closing(self._db._connect()) as conn:
            return {json.loads(r["args"])["proposal_id"]: {"state": r["state"], "error": r["error"]}
                    for r in conn.execute("SELECT args, state, error FROM jobs WHERE user_id = ? AND kind = 'publish'",
                                          (user_id,))}

    def _record_pr(self, user_id: str, proposal_id: str, url: str) -> None:
        with self.store(user_id).transaction() as conn:
            self.proposals(user_id).record_pr_in(conn, proposal_id, url)

    # ── device jobs ───────────────────────────────────────────────────────────

    async def enqueue_device_job(self, user_id: str, kind: str, args: dict, *, request_id: str, rerun: bool,
                                 max_attempts: int = 3) -> Job:
        if kind not in DEVICE_KINDS:
            raise ValueError(f"unknown device job kind {kind!r}")
        job = await self.engine(user_id).enqueue(kind, args, request_id=request_id, rerun=rerun, runner="device",
                                                 max_attempts=max_attempts)
        self._notify(user_id)
        return job

    async def trigger(self, user_id: str, day: str) -> None:
        """The owner asked for `day` from the dashboard: run it even if
        nothing is new. Stage A asks a laptop to synthesize it; stage B asks a
        laptop for fresh notes and synthesizes here once it answers (or after
        OBSERVE_GRACE without it)."""
        if self.stage == "a":
            await self.enqueue_device_job(user_id, "synthesize", {"date": day, "force": True},
                                          request_id=f"trigger-synthesis:{day}", rerun=True)
            return
        await asyncio.to_thread(self._queue_day, user_id, day, "trigger-", True)
        self._notify(user_id)
        self.engine(user_id).wake()

    def _queue_day(self, user_id: str, day: str, prefix: str, force: bool) -> None:
        with self.store(user_id).transaction() as conn:
            self._queue_day_in(conn, user_id, day, prefix, force)

    def _queue_day_in(self, conn: sqlite3.Connection, user_id: str, day: str, prefix: str, force: bool) -> None:
        """Stage B: the server's synthesis of `day`, due after the grace, and
        the device job asking a laptop for fresh notes, which brings it forward."""
        now = self.clock()
        synthesis = f"{prefix}server-synthesis:{day}"
        extra = {"force": True} if force else {}
        job = insert_job(conn, user_id, "synthesize", {"date": day, **extra}, request_id=synthesis,
                         run_after=now + OBSERVE_GRACE, max_attempts=3, now=now, rerun=force)
        if force:   # a rerun is due at once; it still waits for the notes
            conn.execute("UPDATE jobs SET run_after = ? WHERE id = ? AND state = 'queued'",
                         (ts(now + OBSERVE_GRACE), job["id"]))
        insert_job(conn, user_id, "observe", {"date": day, "synthesis": synthesis, **extra},
                   request_id=f"{prefix}observe:{day}", run_after=now, max_attempts=3, now=now, rerun=force,
                   runner="device")

    def queued_dates(self, user_id: str) -> list[str]:
        """Days with a synthesis on its way: asked of a laptop, or (stage B)
        waiting to run here."""
        with closing(self._db._connect()) as conn:
            return sorted({json.loads(r["args"])["date"] for r in conn.execute(
                "SELECT args FROM jobs WHERE user_id = ? AND kind IN ('synthesize', 'observe') "
                "AND state IN ('queued', 'leased', 'waiting')", (user_id,))})

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
        if status.state != "succeeded":
            return await engine.fail_as(job_id, _owner(device_id), f"DeviceJobFailed: {status.error_class or 'unknown'}")
        done = await engine.finish_as(job_id, _owner(device_id), status.model_dump(mode="json"))
        if done and job.kind == "publish" and status.pr_url:
            # The link lives on the proposal; its state stays applied until
            # the laptop sees the branch merged.
            await asyncio.to_thread(self._record_pr, user_id, job.args["proposal_id"], status.pr_url)
        if done and job.kind == "observe":
            await asyncio.to_thread(self._observed, user_id, job.args["synthesis"])
            engine.wake()
        return done

    def _observed(self, user_id: str, synthesis: str) -> None:
        """A laptop sent its fresh notes: the synthesis waiting for them runs
        now; one that already ran (the laptop answered late) runs again."""
        now = self.clock()
        with self.store(user_id).transaction() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE user_id = ? AND request_id = ?",
                               (user_id, synthesis)).fetchone()
            if row is None:
                return
            if row["state"] == "queued":
                conn.execute("UPDATE jobs SET run_after = ?, updated_at = ? WHERE id = ?", (ts(now), ts(now), row["id"]))
            elif row["state"] in ("succeeded", "failed"):
                insert_job(conn, user_id, row["kind"], json.loads(row["args"]), request_id=synthesis, run_after=now,
                           max_attempts=row["max_attempts"], now=now, rerun=True)

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
            if synthesize is not None and self.stage == "b":
                self._queue_day_in(conn, user_id, synthesize, "", False)
            elif synthesize is not None:
                # Same request id as the laptop's own daily run: a day is one
                # job however it was scheduled. (A dashboard trigger is its own
                # job, forced; on the laptop both run the day's one local job.)
                insert_job(conn, user_id, "synthesize", {"date": synthesize}, request_id=f"daily-synthesis:{synthesize}",
                           run_after=now, max_attempts=3, now=now, runner="device")
            insert_job(conn, user_id, "schedule-daily", {"date": day.isoformat()},
                       request_id=f"schedule-daily:{day.isoformat()}",
                       run_after=fire_time(day, user["timezone"], user["daily_at"]), max_attempts=3, now=now)

    async def _schedule_job(self, ctx: JobContext, args: dict) -> dict:
        """The daily timer: have yesterday synthesized (by a device in stage A,
        here in stage B), and set the timer for tomorrow, in one transaction. Wakes may repeat (lesson 5):
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

    async def _telegram_job(self, ctx: JobContext, args: dict) -> dict:
        """Send one message to the user's linked chat. Like a push, the send
        is a step that is not safe to re-run. A proposal message is written at
        send time, from the proposal's target and rationale only, and skipped
        once it is no longer waiting."""
        if self.telegram is None:
            return {"sent": 0, "telegram": "off"}
        user_id = ctx.engine.store.user_id
        chat_id = await ctx.step("chat", lambda: asyncio.to_thread(self._telegram_chat, user_id),
                                 replay_safe=True, output=int | None)
        if chat_id is None:
            return {"sent": 0, "telegram": "unlinked"}
        buttons = None
        if proposal_id := args.get("proposal_id"):
            proposal = await self.proposals(user_id).get(proposal_id)
            if proposal is None or proposal.state != "proposed":
                return {"sent": 0, "state": proposal.state if proposal else None}
            text, buttons = proposal_message(proposal_id, proposal.target, proposal.payload["rationale"],
                                             self.public_url)
        else:
            text = args["text"] + (f"\n\n{self.public_url}{args['url']}" if args.get("url") and self.public_url
                                   else "")
        await ctx.step("send", lambda: self.telegram.send(chat_id, text, buttons), replay_safe=False)
        return {"sent": 1}
