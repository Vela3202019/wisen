"""
`wisen worker`: the laptop's daemon for a paired server. It only connects out
(launchd keeps it alive; the laptop never opens a port):

  1. sync up    each day's reflection as a result packet, and each open
                proposal, when it changed since the last upload
  2. poll       one long poll returns a device job to run and any decisions
                made since the last one
  3. decisions  recorded in the local proposal table (the server decides; the
                first decision is kept) and signalled to any job waiting on it
  4. jobs       run here, heartbeating the server's lease, then reported back
                as a job_status packet

What it last uploaded and how far it has read the decision feed are stored in
the local SQLite file, so a restart picks up where it stopped.
"""
import asyncio
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
from wisen.jobs import LEASE, ts
from wisen.packets import job_status_packet, proposal_packet, result_packet
from wisen.proposals import DECISIONS, IllegalTransition, Proposals
from wisen.store import SqliteStore

SYNC_EVERY = 300.0  # seconds between sync-up passes while idle
BACKOFF_CAP = 60.0


class SynthesisFailed(Exception):
    pass


class UnknownJobKind(Exception):
    pass


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


async def sync_up(client: CloudClient, store: SqliteStore) -> int:
    """Upload every result and open proposal that changed since its last
    upload; the server ignores a packet it already holds anyway."""
    creds, sent = client.creds, 0
    for doc in await store.list_reflections():
        if date := doc.get("date"):
            sent += await _upload(client, store, f"sent:result:{date}", lambda doc=doc: result_packet(
                doc, user=creds.user_id, device_id=creds.device_id))
    for p in await Proposals(store).list(states=("proposed",)):
        sent += await _upload(client, store, f"sent:proposal:{p.id}", lambda p=p: proposal_packet(
            p.id, p.target, p.payload, p.expires_at, user=creds.user_id, device_id=creds.device_id))
    return sent


# ── 3. decisions ──────────────────────────────────────────────────────────────

async def apply_decisions(store: SqliteStore, decisions: list[dict]) -> None:
    """Record the server's decisions locally. Each carries the server's request
    id, so a decision seen twice (a replayed feed) changes nothing."""
    from wisen.jobs import get_engine

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


# ── 4. device jobs ────────────────────────────────────────────────────────────

async def _synthesize(client: CloudClient, store: SqliteStore, args: dict) -> None:
    from wisen.synthesis.job import synthesize_day

    # The day's one local job (request id daily-synthesis:<date>): a run the
    # laptop already started resumes from its last checkpoint.
    job = await synthesize_day(args["date"])
    if job.state != "succeeded":
        raise SynthesisFailed(job.error)
    doc = await store.get_reflection(args["date"])
    creds = client.creds
    await _upload(client, store, f"sent:result:{args['date']}", lambda: result_packet(
        doc, user=creds.user_id, device_id=creds.device_id))


HANDLERS = {"synthesize": _synthesize}


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
    error = None
    try:
        handler = HANDLERS.get(job["kind"])
        if handler is None:
            raise UnknownJobKind(job["kind"])
        await handler(client, store, job["args"])
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
    }, user=client.creds.user_id, device_id=client.creds.device_id)
    try:
        await client.finish(job["id"], status)
    except Refused as e:
        print(f"[worker] {job['kind']} job result not accepted: {e}", file=sys.stderr)
    return state


# ── the loop ──────────────────────────────────────────────────────────────────

async def run(creds: Credentials, store: SqliteStore, *, http=None, once: bool = False) -> None:
    """Poll forever (or one pass with `once`, which does not wait). Returns
    when the server revokes this device's token."""
    cursor = int(await asyncio.to_thread(_get, store, "decisions_cursor") or 0)
    last_sync, backoff = float("-inf"), 1.0
    async with CloudClient(creds, http) as client:
        while True:
            try:
                if once or time.monotonic() - last_sync >= SYNC_EVERY:
                    await sync_up(client, store)
                    last_sync = time.monotonic()
                reply = await client.poll(cursor, wait=0 if once else POLL_WAIT)
                if reply["decisions"]:
                    await apply_decisions(store, reply["decisions"])
                    cursor = reply["cursor"]
                    await asyncio.to_thread(_set, store, "decisions_cursor", str(cursor))
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
