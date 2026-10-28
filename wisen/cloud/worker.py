"""
`wisen worker`: the laptop's daemon for a paired server. It only connects out
(launchd keeps it alive; the laptop never opens a port):

  1. sync up    proposals not uploaded yet first (so a day's batch is there
                before its reflection's push), each with its decision if made
                while unpaired, then the git states the laptop
                decided for proposals (applied, merged, measured, …), each day's metrics,
                and each day's reflection, when it changed since the last upload
  2. poll       one long poll returns a device job to run and any decisions
                made since the last one
  3. decisions  recorded in the local proposal table (the server decides; the
                first decision is kept), signalled to any job waiting on it,
                and turned into Gardener jobs (apply, resume)
  4. jobs       run here, heartbeating the server's lease, then reported back
                as a job_status packet: the daily synthesis, a measure, the
                owner's "Push & create PR" for an applied proposal
                (wisen/gardener/publisher.py), whose status carries the
                pull request's link, and a question asked in Telegram
                (answered by the chat agent and sent back as an answer packet)

It also runs the laptop's own job engine (the Gardener's jobs and timers) and
checks applied Gardener branches for merges. Unpaired, `run_local` does only
that: local-only mode.

What it last uploaded and how far it has read the decision feed are stored in
the local SQLite file, so a restart picks up where it stopped.
"""
import asyncio
import json
import sys
import time
from contextlib import closing, suppress
from datetime import UTC, datetime

from wisen.cloud.client import (
    POLL_WAIT,
    CloudClient,
    Credentials,
    Refused,
    Revoked,
    Unavailable,
)
from wisen.egress import EgressRejected
from wisen.jobs import LEASE, JobEngine, get_engine, ts
from wisen.packets import (
    answer_packet,
    job_status_packet,
    metrics_packet,
    proposal_packet,
    proposal_update_packet,
    result_packet,
)
from wisen.proposals import DECISIONS, IllegalTransition, Proposal, Proposals, _proposal
from wisen.store import SqliteStore

SYNC_EVERY = 300.0  # seconds between sync-up passes while idle
BACKOFF_CAP = 60.0


class SynthesisFailed(Exception):
    pass


class UnknownJobKind(Exception):
    pass


class MeasureFailed(Exception):
    pass


# The moves the laptop is the authority for (cloud-architecture §6.1).
GIT_STATES = ("applied", "failed", "merged", "abandoned", "measuring", "kept", "revert_proposed")


# ── local bookkeeping ─────────────────────────────────────────────────────────

def _get(store: SqliteStore, key: str) -> str | None:
    with closing(store._connect()) as conn:
        row = conn.execute("SELECT value FROM cloud_state WHERE user_id = ? AND key = ?", (store.user_id, key)).fetchone()
    return row["value"] if row else None


def _set(store: SqliteStore, key: str, value: str) -> None:
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO cloud_state (user_id, key, value, at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (user_id, key) DO UPDATE SET value = excluded.value, at = excluded.at",
            (store.user_id, key, value, ts(datetime.now(UTC))),
        )


# ── 1. sync up ────────────────────────────────────────────────────────────────

async def _upload(client: CloudClient, store: SqliteStore, key: str, build) -> bool:
    try:
        packet = build()
        if await asyncio.to_thread(_get, store, key) == packet["packet_id"]:
            return False
        await client.send_packet(packet)
    except (EgressRejected, Refused) as e:
        # Never the packet's text: the error names fields only.
        print(f"[worker] not sent ({key}): {e}", file=sys.stderr)
        return False
    await asyncio.to_thread(_set, store, key, packet["packet_id"])
    return True


def _unsent(store: SqliteStore) -> list[tuple[Proposal, dict | None]]:
    """Proposals not uploaded yet, oldest first, leaving out those kept local,
    each with its decision or expiry if it has one (made while unpaired)."""
    with closing(store._connect()) as conn:
        return [(_proposal(r), {"request_id": r["request_id"], "state": r["to_state"], "at": r["at"],
                                "note": r["note"]} if r["to_state"] else None) for r in conn.execute(
            "SELECT p.*, e.request_id, e.to_state, e.at, e.note FROM proposals p "
            "LEFT JOIN proposal_events e ON e.user_id = p.user_id AND e.proposal_id = p.id "
            "AND e.from_state = 'proposed' WHERE p.user_id = ? AND NOT EXISTS (SELECT 1 FROM cloud_state c "
            "WHERE c.user_id = p.user_id AND c.key IN ('sent:proposal:' || p.id, 'local:proposal:' || p.id)) "
            "ORDER BY p.created_at", (store.user_id,))]


def _updates(store: SqliteStore) -> list[dict]:
    """Every git-state move of an uploaded proposal, oldest first, as update
    content."""
    with closing(store._connect()) as conn:
        rows = conn.execute(
            f"SELECT e.proposal_id, e.request_id, e.from_state, e.to_state, e.note, e.at, g.branch, m.result "
            f"FROM proposal_events e LEFT JOIN proposal_git g ON g.proposal_id = e.proposal_id "
            f"LEFT JOIN proposal_measures m ON m.proposal_id = e.proposal_id "
            f"WHERE e.user_id = ? AND e.to_state IN ({', '.join('?' * len(GIT_STATES))}) "
            f"AND NOT EXISTS (SELECT 1 FROM cloud_state c "
            f"WHERE c.user_id = e.user_id AND c.key = 'local:proposal:' || e.proposal_id) ORDER BY e.id",
            (store.user_id, *GIT_STATES),
        ).fetchall()
    updates = []
    for r in rows:
        measure = None
        if r["to_state"] in ("kept", "revert_proposed") and r["result"]:
            result = json.loads(r["result"])
            measure = {k: result[k] for k in ("verdict", "before", "after")}
        updates.append({"proposal_id": r["proposal_id"], "request_id": r["request_id"],
                        "from_state": r["from_state"], "to_state": r["to_state"], "at": r["at"],
                        "branch": r["branch"] if r["to_state"] in ("applied", "merged") else None,
                        "note": r["note"] if r["to_state"] in ("failed", "kept", "revert_proposed") else None,
                        "measure": measure})
    return updates


def _metrics(store: SqliteStore) -> list[dict]:
    with closing(store._connect()) as conn:
        return [json.loads(r["doc"]) for r in conn.execute(
            "SELECT doc FROM daily_metrics WHERE user_id = ? ORDER BY date", (store.user_id,))]


async def sync_up(client: CloudClient, store: SqliteStore) -> int:
    """Upload everything that changed since its last upload; the server
    ignores a packet it already holds anyway. Proposals go first, so a day's
    batch is in the inbox before that day's reflection announces it."""
    creds, sent = client.creds, 0
    who = {"user": creds.user_id, "device_id": creds.device_id}
    for p, decided in await asyncio.to_thread(_unsent, store):
        try:
            await client.send_packet(proposal_packet(p.id, p.target, p.payload, p.expires_at, decided=decided,
                                                     **who))
        except EgressRejected as e:
            # It stays local, and so do its moves: recorded once, never retried.
            await asyncio.to_thread(_set, store, f"local:proposal:{p.id}", str(e))
            print(f"[worker] proposal {p.id} stays local: {e}", file=sys.stderr)
            continue
        except Refused as e:
            print(f"[worker] not sent (proposal {p.id}): {e}", file=sys.stderr)
            continue
        await asyncio.to_thread(_set, store, f"sent:proposal:{p.id}", ts(datetime.now(UTC)))
        sent += 1
    stuck: set[str] = set()  # a refused move holds back the later moves of its proposal
    for u in await asyncio.to_thread(_updates, store):
        if u["proposal_id"] in stuck:
            continue
        key = f"sent:update:{u['request_id']}"
        sent += await _upload(client, store, key, lambda u=u: proposal_update_packet(u, **who))
        if await asyncio.to_thread(_get, store, key) is None:
            stuck.add(u["proposal_id"])
    for doc in await asyncio.to_thread(_metrics, store):
        sent += await _upload(client, store, f"sent:metrics:{doc['date']}", lambda doc=doc: metrics_packet(
            doc, **who))
    for doc in await store.list_reflections():
        if date := doc.get("date"):
            sent += await _upload(client, store, f"sent:result:{date}", lambda doc=doc: result_packet(doc, **who))
    return sent


# ── 3. decisions ──────────────────────────────────────────────────────────────

async def apply_decisions(store: SqliteStore, decisions: list[dict]) -> None:
    """Record the server's decisions locally. Each carries the server's request
    id, so a decision seen twice (a replayed feed) changes nothing."""
    from wisen.gardener.loop import on_decided

    proposals = Proposals(store)
    for d in decisions:
        try:
            if d["state"] in DECISIONS:
                await proposals.decide(d["proposal_id"], d["state"], request_id=d["request_id"],
                                       actor=d["actor"], note=d["note"])
            else:
                await proposals.transition(d["proposal_id"], "proposed", d["state"], request_id=d["request_id"],
                                           actor=d["actor"], note=d["note"])
        except (IllegalTransition, ValueError) as e:
            print(f"[worker] decision on {d['proposal_id']} not recorded: {e}", file=sys.stderr)
        # A job parked on this proposal (ctx.wait) resumes with the decision.
        await get_engine().signal(f"proposal:{d['proposal_id']}", {"state": d["state"], "note": d["note"]})
        # An approval queues the applier; a run with nothing left undecided resumes.
        await on_decided(store, get_engine(), d["proposal_id"])


# ── 4. device jobs ────────────────────────────────────────────────────────────

async def _synthesize(client: CloudClient, store: SqliteStore, job: dict) -> dict | None:
    from wisen.synthesis.job import Skipped, garden_day, synthesize_day

    args = job["args"]
    # The day's one local job (request id daily-synthesis:<date>): a run the
    # laptop already started resumes from its last checkpoint. A day with
    # nothing new is left alone, and the job status says so, unless the owner
    # asked for the day from the dashboard (`force`).
    job = await synthesize_day(args["date"], force=args.get("force", False))
    if isinstance(job, Skipped):
        print(f"[worker] synthesize {job.date} skipped: {job.reason}")
        await sync_up(client, store)
        return {"skipped": job.reason}
    if job.state != "succeeded":
        raise SynthesisFailed(job.error)
    # The day's Gardener batch, so it arrives with the morning reflection.
    garden = await garden_day(args["date"])
    if garden.state != "succeeded":
        print(f"[worker] gardener failed for {args['date']}: {garden.error}", file=sys.stderr)
    await sync_up(client, store)
    return None


async def _measure(client: CloudClient, store: SqliteStore, job: dict) -> None:
    """The server's timer for a merged proposal. The laptop set the same
    timer when it saw the merge (request id measure:<id>), so this runs that
    one job, or finds it done."""
    engine = get_engine()
    pid = job["args"]["proposal_id"]
    measure = await engine.enqueue("measure-proposal", {"proposal_id": pid}, request_id=f"measure:{pid}")
    measure = await engine.run_until_done(measure.id)
    if measure.state != "succeeded":
        raise MeasureFailed(measure.error)
    await sync_up(client, store)


async def _publish(client: CloudClient, store: SqliteStore, job: dict) -> dict:
    """The owner's "Push & create PR": push the applied branch and open its
    pull request here, where the repo and the gh login are. The link goes
    back in the job status."""
    from wisen.gardener.publisher import publish

    return {"pr_url": await asyncio.to_thread(publish, store, job["args"]["proposal_id"])}


async def _answer(client: CloudClient, store: SqliteStore, job: dict) -> None:
    """A question from Telegram: the chat agent answers it here, from
    reflections, or also from the raw history for /search (kind `search`).
    Only the written answer leaves, as an answer packet."""
    from wisen.chat import agent as chat

    text = await chat.answer(job["args"]["question"], raw_search=job["kind"] == "search")
    await client.send_packet(answer_packet(job["id"], text, user=client.creds.user_id,
                                           device_id=client.creds.device_id))


HANDLERS = {"synthesize": _synthesize, "measure": _measure, "publish": _publish, "ask": _answer, "search": _answer}


async def _heartbeat(client: CloudClient, job_id: str) -> None:
    while True:
        await asyncio.sleep(LEASE.total_seconds() / 3)
        try:
            await client.heartbeat(job_id)
        except Refused:
            return  # the lease moved on; finishing will be refused too
        except Unavailable:
            continue  # keep working; the lease may still hold when the server is back


async def run_device_job(client: CloudClient, store: SqliteStore, job: dict) -> str:
    """Run one leased device job and report how it ended. Returns the state."""
    started = datetime.now(UTC)
    beat = asyncio.create_task(_heartbeat(client, job["id"]))
    error, outcome = None, None
    try:
        handler = HANDLERS.get(job["kind"])
        if handler is None:
            raise UnknownJobKind(job["kind"])
        outcome = await handler(client, store, job)  # extra job status fields, if any
    except Revoked:
        raise
    except Exception as e:  # noqa: BLE001 - every failure is reported; the server retries
        error = type(e).__name__
        print(f"[worker] {job['kind']} job failed: {error}: {e}", file=sys.stderr)
    finally:
        beat.cancel()
        with suppress(asyncio.CancelledError):
            await beat
    state = "failed" if error else "succeeded"
    status = job_status_packet({
        "job_id": job["id"], "kind": job["kind"], "state": state, "error_class": error,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        **(outcome or {}),
    }, user=client.creds.user_id, device_id=client.creds.device_id)
    try:
        await client.finish(job["id"], status)
    except Refused as e:
        print(f"[worker] {job['kind']} job result not accepted: {e}", file=sys.stderr)
    return state


# ── the loop ──────────────────────────────────────────────────────────────────

async def tend(store: SqliteStore, engine: JobEngine) -> None:
    """Check applied Gardener branches for a merge (which sets the measure timer)."""
    from wisen.gardener.applier import detect_merges

    moved = await asyncio.to_thread(detect_merges, store, Proposals(store, engine.clock))
    if moved:
        engine.wake()


async def drain(engine: JobEngine) -> None:
    """Run every local job that is due now."""
    while await engine.run_once():
        pass


async def run_local(store: SqliteStore) -> None:
    """Local-only mode: the laptop's job engine (Gardener timers included)
    and merge checks, with no server."""
    engine = get_engine()
    local = asyncio.create_task(engine.run_forever())
    try:
        while True:
            await tend(store, engine)
            await asyncio.sleep(SYNC_EVERY)
    finally:
        local.cancel()


async def run(creds: Credentials, store: SqliteStore, *, http=None, once: bool = False) -> None:
    """Poll forever (or one pass with `once`, which does not wait). Returns
    when the server revokes this device's token."""
    engine = get_engine()
    local = None if once else asyncio.create_task(engine.run_forever())
    try:
        await _poll_loop(creds, store, engine, http, once)
    finally:
        if local:
            local.cancel()


async def _poll_loop(creds: Credentials, store: SqliteStore, engine: JobEngine, http, once: bool) -> None:
    cursor = int(await asyncio.to_thread(_get, store, "decisions_cursor") or 0)
    last_sync, backoff = float("-inf"), 1.0
    async with CloudClient(creds, http) as client:
        while True:
            try:
                if once or time.monotonic() - last_sync >= SYNC_EVERY:
                    await tend(store, engine)
                    await sync_up(client, store)
                    last_sync = time.monotonic()
                reply = await client.poll(cursor, wait=0 if once else POLL_WAIT)
                if reply["decisions"]:
                    await apply_decisions(store, reply["decisions"])
                    cursor = reply["cursor"]
                    await asyncio.to_thread(_set, store, "decisions_cursor", str(cursor))
                if once:
                    await drain(engine)
                if reply["job"]:
                    await run_device_job(client, store, reply["job"])
                backoff = 1.0
            except Revoked as e:
                print(f"[worker] {e}", file=sys.stderr)
                return
            except (Unavailable, Refused) as e:
                if once:
                    raise
                print(f"[worker] {e}; retrying in {backoff:.0f}s", file=sys.stderr)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_CAP)
            if once:
                return
