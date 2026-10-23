"""
Proposal state machine: the record of each Gardener proposal and the only code
that moves it between states. The Gardener itself (what to propose, applying
the branch, measuring) is wisen/gardener/; this is the record and its
transitions.

    proposed  --> approved | rejected   a human decision, through decide()
    proposed  --> expired               no decision before expires_at
    proposed  --> superseded            a newer proposal for the same file
    approved  --> applied | failed      the laptop committed the branch, or could not
    applied   --> merged | abandoned    the branch reached the default branch, or was deleted
    merged    --> measuring             N weeks after merge
    measuring --> kept | revert_proposed

Every move is a compare-and-set on the current state plus an audit row in
`proposal_events`, in one transaction (lesson 1 in wisen/jobs.py). Each
command carries a request id and its audit row is the receipt, so a resent
command returns the original result instead of acting twice (lesson 4). A
decision is also a first-write-wins memo (lesson 6): the first approve or
reject is recorded once, and a later one (a Telegram retry, a tap on a second
device) gets the first answer back. See docs/plan/cloud-architecture.md §6.1.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from wisen.jobs import JobContext, insert_job, ts
from wisen.store import SqliteStore

# The one place allowed moves are defined. States with no entry are final.
TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"approved", "rejected", "expired", "superseded"}),
    "approved": frozenset({"applied", "failed"}),
    "applied": frozenset({"merged", "abandoned"}),
    "merged": frozenset({"measuring"}),
    "measuring": frozenset({"kept", "revert_proposed"}),
}
DECISIONS = frozenset({"approved", "rejected"})
# The states the human's side decides (the server, once paired); the laptop
# learns them from the decision feed (`events_since`).
DECIDED = ("approved", "rejected", "expired", "superseded")


class IllegalTransition(Exception):
    """The move is not allowed from the proposal's current state; nothing changed."""


@dataclass
class Proposal:
    id: str
    target: str
    state: str
    payload: dict
    expires_at: str | None
    created_at: str
    updated_at: str


@dataclass
class ProposalEvent:
    proposal_id: str
    request_id: str
    from_state: str | None
    to_state: str
    actor: str
    note: str | None
    at: str


@dataclass
class Decision:
    proposal_id: str
    decision: str
    actor: str
    note: str | None
    request_id: str
    at: str


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _proposal(row: sqlite3.Row) -> Proposal:
    return Proposal(
        id=row["id"], target=row["target"], state=row["state"], payload=json.loads(row["payload"]),
        expires_at=row["expires_at"], created_at=row["created_at"], updated_at=row["updated_at"],
    )


def _event(row: sqlite3.Row) -> ProposalEvent:
    return ProposalEvent(
        proposal_id=row["proposal_id"], request_id=row["request_id"], from_state=row["from_state"],
        to_state=row["to_state"], actor=row["actor"], note=row["note"], at=row["at"],
    )


def _decision(row: sqlite3.Row) -> Decision:
    return Decision(
        proposal_id=row["proposal_id"], decision=row["decision"], actor=row["actor"],
        note=row["note"], request_id=row["request_id"], at=row["at"],
    )


class Proposals:
    def __init__(self, store: SqliteStore, clock: Callable[[], datetime] = _utcnow):
        self.store = store
        self.clock = clock

    @property
    def _uid(self) -> str:
        return self.store.user_id

    def _receipt(self, conn: sqlite3.Connection, request_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM proposal_events WHERE user_id = ? AND request_id = ?", (self._uid, request_id)
        ).fetchone()

    def _move(
        self, conn: sqlite3.Connection, proposal_id: str, from_state: str, to_state: str,
        request_id: str, actor: str, note: str | None, now: str,
    ) -> ProposalEvent:
        if to_state not in TRANSITIONS.get(from_state, ()):
            raise IllegalTransition(f"{from_state} -> {to_state} is not an allowed move")
        sql = "UPDATE proposals SET state = ?, updated_at = ? WHERE user_id = ? AND id = ? AND state = ?"
        params: list = [to_state, now, self._uid, proposal_id, from_state]
        if from_state == "proposed" and to_state != "expired":
            # Past its deadline a proposal can only expire, even before its
            # expiry timer has fired.
            sql += " AND (expires_at IS NULL OR expires_at > ?)"
            params.append(now)
        if conn.execute(sql, params).rowcount != 1:
            row = conn.execute(
                "SELECT state FROM proposals WHERE user_id = ? AND id = ?", (self._uid, proposal_id)
            ).fetchone()
            if row is None:
                raise IllegalTransition(f"no proposal {proposal_id}")
            if row["state"] != from_state:
                raise IllegalTransition(f"proposal {proposal_id} is {row['state']}, not {from_state}")
            raise IllegalTransition(f"proposal {proposal_id} is past its expiry")
        conn.execute(
            "INSERT INTO proposal_events (user_id, proposal_id, request_id, from_state, to_state, actor, note, at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self._uid, proposal_id, request_id, from_state, to_state, actor, note, now),
        )
        return ProposalEvent(proposal_id, request_id, from_state, to_state, actor, note, now)

    # ── create / read ─────────────────────────────────────────────────────────

    def _create(self, target, payload, request_id, actor, expires_at, proposal_id) -> Proposal:
        with self.store.transaction() as conn:
            return self.create_in(conn, target, payload, request_id, actor, expires_at, proposal_id)

    def create_in(
        self, conn: sqlite3.Connection, target: str, payload: dict, request_id: str, actor: str,
        expires_at: datetime | None, proposal_id: str | None = None,
    ) -> Proposal:
        """`create`, inside the caller's transaction (lesson 1), so the record
        commits with whatever else that transaction writes."""
        now = self.clock()
        receipt = self._receipt(conn, request_id)
        if receipt:
            if proposal_id not in (None, receipt["proposal_id"]):
                raise ValueError(f"request id {request_id!r} was already used for a different command")
            proposal_id = receipt["proposal_id"]
            row = conn.execute("SELECT target, payload FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
            if receipt["from_state"] is not None or (row["target"], json.loads(row["payload"])) != (
                target, payload
            ):
                raise ValueError(f"request id {request_id!r} was already used for a different command")
        else:
            if proposal_id is None:
                proposal_id = uuid.uuid4().hex
            elif conn.execute("SELECT 1 FROM proposals WHERE id = ?", (proposal_id,)).fetchone():
                raise ValueError(f"proposal {proposal_id} already exists")
            conn.execute(
                "INSERT INTO proposals (id, user_id, target, state, payload, expires_at, created_at, updated_at) "
                "VALUES (?, ?, ?, 'proposed', ?, ?, ?, ?)",
                (proposal_id, self._uid, target, json.dumps(payload), ts(expires_at) if expires_at else None,
                 ts(now), ts(now)),
            )
            conn.execute(
                "INSERT INTO proposal_events (user_id, proposal_id, request_id, from_state, to_state, actor, at) "
                "VALUES (?, ?, ?, NULL, 'proposed', ?, ?)",
                (self._uid, proposal_id, request_id, actor, ts(now)),
            )
            if expires_at:
                # The expiry is a stored timer job written with the
                # proposal, so no restart can lose it (lessons 1 and 5).
                insert_job(
                    conn, self._uid, "expire-proposal", {"proposal_id": proposal_id},
                    request_id=f"expire-proposal:{proposal_id}", run_after=expires_at,
                    max_attempts=3, now=now,
                )
        row = conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        return _proposal(row)

    async def create(
        self, target: str, payload: dict[str, Any], *, request_id: str, actor: str,
        expires_at: datetime | None = None, proposal_id: str | None = None,
    ) -> Proposal:
        """Record a new proposal in `proposed`. `target` names the file it
        changes; `payload` holds the rest (diff, rationale, evidence).
        `proposal_id` keeps an id given elsewhere (the server records a
        laptop's proposal under the laptop's id); by default one is made."""
        return await asyncio.to_thread(
            self._create, target, payload, request_id, actor, expires_at, proposal_id
        )

    def _get(self, proposal_id: str) -> Proposal | None:
        with closing(self.store._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM proposals WHERE user_id = ? AND id = ?", (self._uid, proposal_id)
            ).fetchone()
        return _proposal(row) if row else None

    async def get(self, proposal_id: str) -> Proposal | None:
        return await asyncio.to_thread(self._get, proposal_id)

    def _history(self, proposal_id: str) -> list[ProposalEvent]:
        with closing(self.store._connect()) as conn:
            return [_event(r) for r in conn.execute(
                "SELECT * FROM proposal_events WHERE user_id = ? AND proposal_id = ? ORDER BY id",
                (self._uid, proposal_id),
            )]

    async def history(self, proposal_id: str) -> list[ProposalEvent]:
        """The audit trail: every move, oldest first."""
        return await asyncio.to_thread(self._history, proposal_id)

    def _list(self, states: tuple[str, ...] | None, limit: int) -> list[Proposal]:
        sql, params = "SELECT * FROM proposals WHERE user_id = ?", [self._uid]
        if states is not None:
            sql += f" AND state IN ({', '.join('?' * len(states))})"
            params += states
        with closing(self.store._connect()) as conn:
            return [_proposal(r) for r in conn.execute(sql + " ORDER BY created_at DESC LIMIT ?", (*params, limit))]

    async def list(self, states: tuple[str, ...] | None = None, limit: int = 50) -> list[Proposal]:
        """Newest first, optionally only those in `states`."""
        return await asyncio.to_thread(self._list, states, limit)

    def _events_since(self, cursor: int, to_states: tuple[str, ...], limit: int) -> list[tuple[int, ProposalEvent]]:
        with closing(self.store._connect()) as conn:
            return [(r["id"], _event(r)) for r in conn.execute(
                f"SELECT * FROM proposal_events WHERE user_id = ? AND id > ? "
                f"AND to_state IN ({', '.join('?' * len(to_states))}) ORDER BY id LIMIT ?",
                (self._uid, cursor, *to_states, limit),
            )]

    async def events_since(
        self, cursor: int, to_states: tuple[str, ...] = DECIDED, limit: int = 100,
    ) -> list[tuple[int, ProposalEvent]]:
        """Committed moves into `to_states` after event id `cursor`, oldest
        first, each with its event id (the next cursor). From cursor 0 this is
        the full snapshot, so a reconnecting reader catches up with one call
        (lesson 8: reconnect is snapshot, then committed changes)."""
        return await asyncio.to_thread(self._events_since, cursor, to_states, limit)

    # ── moves ─────────────────────────────────────────────────────────────────

    def _decide(self, proposal_id, decision, request_id, actor, note) -> Decision:
        if decision not in DECISIONS:
            raise ValueError(f"a decision is one of {sorted(DECISIONS)}, not {decision!r}")
        now = ts(self.clock())
        with self.store.transaction() as conn:
            receipt = self._receipt(conn, request_id)
            if receipt and (receipt["proposal_id"], receipt["from_state"], receipt["to_state"]) != (
                proposal_id, "proposed", decision
            ):
                raise ValueError(f"request id {request_id!r} was already used for a different command")
            memo = conn.execute(
                "SELECT * FROM proposal_decisions WHERE user_id = ? AND proposal_id = ?",
                (self._uid, proposal_id),
            ).fetchone()
            if memo is None:
                self._move(conn, proposal_id, "proposed", decision, request_id, actor, note, now)
                conn.execute(
                    "INSERT INTO proposal_decisions (proposal_id, user_id, decision, actor, note, request_id, at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (proposal_id, self._uid, decision, actor, note, request_id, now),
                )
                memo = conn.execute(
                    "SELECT * FROM proposal_decisions WHERE proposal_id = ?", (proposal_id,)
                ).fetchone()
        return _decision(memo)

    async def decide(
        self, proposal_id: str, decision: str, *, request_id: str, actor: str, note: str | None = None,
    ) -> Decision:
        """Approve or reject. The first decision wins and is never
        overwritten: a later call returns it, so a caller whose request id
        differs from the returned one knows it was already decided."""
        return await asyncio.to_thread(self._decide, proposal_id, decision, request_id, actor, note)

    def _transition(self, proposal_id, from_state, to_state, request_id, actor, note) -> ProposalEvent:
        with self.store.transaction() as conn:
            return self.transition_in(conn, proposal_id, from_state, to_state, request_id, actor, note)

    def transition_in(
        self, conn: sqlite3.Connection, proposal_id: str, from_state: str, to_state: str, request_id: str,
        actor: str, note: str | None = None,
    ) -> ProposalEvent:
        """`transition`, inside the caller's transaction (lesson 1), so the
        move commits with whatever else that transaction writes."""
        if to_state in DECISIONS:
            raise IllegalTransition(f"{to_state} is a decision; use decide()")
        receipt = self._receipt(conn, request_id)
        if receipt:
            if (receipt["proposal_id"], receipt["from_state"], receipt["to_state"]) != (
                proposal_id, from_state, to_state
            ):
                raise ValueError(f"request id {request_id!r} was already used for a different move")
            return _event(receipt)
        return self._move(conn, proposal_id, from_state, to_state, request_id, actor, note, ts(self.clock()))

    async def transition(
        self, proposal_id: str, from_state: str, to_state: str, *, request_id: str, actor: str,
        note: str | None = None,
    ) -> ProposalEvent:
        """Move a proposal from `from_state` to `to_state`, or raise
        IllegalTransition and change nothing."""
        return await asyncio.to_thread(
            self._transition, proposal_id, from_state, to_state, request_id, actor, note
        )

    def _expire(self, proposal_id: str) -> bool:
        now = ts(self.clock())
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT state, expires_at FROM proposals WHERE user_id = ? AND id = ?", (self._uid, proposal_id)
            ).fetchone()
            # Already decided, or a timer that fired early: nothing to do. Timer
            # wakes are at-least-once, so this must be safe to call again.
            if row is None or row["state"] != "proposed" or row["expires_at"] is None or row["expires_at"] > now:
                return False
            self._move(conn, proposal_id, "proposed", "expired", f"expire:{proposal_id}", "system", None, now)
        return True

    async def expire(self, proposal_id: str) -> bool:
        """Expire the proposal if it is still undecided past its deadline."""
        return await asyncio.to_thread(self._expire, proposal_id)


async def expire_proposal_job(ctx: JobContext, args: dict) -> dict:
    """Handler for the `expire-proposal` timer job that `create` schedules."""
    expired = await Proposals(ctx.engine.store, ctx.engine.clock).expire(args["proposal_id"])
    return {"expired": expired}
