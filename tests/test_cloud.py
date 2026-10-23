"""
Server stage A, end to end in one process: the server app runs in-process
(httpx ASGI transport) on temp SQLite files, the laptop side is the real
`wisen worker` code, push goes to a fake sender, and the model is a fake.
Covers the step-4 checks that do not need the box:

  - login: the dashboard and its API need a session; logins are throttled
  - pairing: a one-time code buys a device token; only its hash is stored
  - a revoked device token gets 401, and its leased job is released
  - a day's reflection syncs laptop -> server and triggers one push
  - approvals: a proposal syncs up, the first decision wins, the laptop learns it
  - a server restart mid-lease recovers the device job; a stale lease is fenced
  - the daily schedule is a stored timer that asks a device to synthesize
  - backup, then restore, serves the same data
  - the server rejects packets off the schema, unredacted or mis-addressed
  - no model key: a full server flow never imports a model library
  - after a full laptop -> server sync of the fixtures, no fixture marker
    string appears in the server's SQLite files

No network or model needed. Run: python3 -m tests.test_cloud
"""
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

_TMP = Path(tempfile.mkdtemp())
_FIXTURES = Path(__file__).resolve().parent / "fixtures"
# Point the laptop side at temp files and the committed fixture sessions
# before wisen is imported (connectors read WISEN_ROOT_DIR at import time).
os.environ.update({
    "WISEN_DB_PATH": str(_TMP / "laptop.db"),
    "WISEN_USER_ID": "local",
    "WISEN_EGRESS_LOG": str(_TMP / "egress.log"),
    "WISEN_ROOT_DIR": str(_FIXTURES),
    "GIT_SCAN_DIRS": str(_FIXTURES / "repos"),
})
os.environ.pop("GITHUB_TOKEN", None)  # the git connector stays offline

import httpx

from tests.test_egress import (
    ABS_PATH,
    EMAIL,
    FAKE_KEY,
    MARKERS,
    _poisoned_doc,
    _sensitive_doc,
)
from wisen import egress
from wisen.cloud import worker
from wisen.cloud.backup import backup, restore
from wisen.cloud.client import (
    CloudClient,
    Credentials,
    Refused,
    Revoked,
    pair,
)
from wisen.cloud.mailbox import Mailbox
from wisen.cloud.push import Gone
from wisen.cloud.server import create_app
from wisen.jobs import LEASE
from wisen.packets import (
    content_id,
    job_status_packet,
    proposal_packet,
    result_packet,
)
from wisen.proposals import Proposals
from wisen.store import get_store
from wisen.synthesis import agent
from wisen.synthesis import job as synthesis_job
from wisen.synthesis.schema import DayObservation, Reflection

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
USER, PASSWORD = "vela", "correct horse battery"
BASE = "http://localhost"
FIXTURE_DATE = "2026-01-15"
# Raw text from the committed fixture sessions; it reaches the (fake) model,
# and must never reach the server.
RAW = [
    "add pagination to the /items endpoint",
    "write a migration that backfills next_cursor",
    "write a csv parser for the import command",
    "return db.items.find()",
]


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class FakeSender:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []
        self.gone: set[str] = set()

    async def send(self, subscription: dict, payload: str) -> None:
        if subscription["endpoint"] in self.gone:
            raise Gone
        self.sent.append((subscription["endpoint"], json.loads(payload)))


class Server:
    """One server process: a mailbox on `db` and its app. A new Server on the
    same file is a restart."""

    def __init__(self, db: Path, clock: Clock, sender: FakeSender | None = None):
        self.db, self.clock, self.sender = db, clock, sender or FakeSender()
        self.mailbox = Mailbox(db, sender=self.sender, clock=clock)
        self.app = create_app(self.mailbox, vapid_public_key="test-key", secure_cookies=False, run_engines=False)

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=BASE)

    @asynccontextmanager
    async def browser(self, user: str = USER, password: str = PASSWORD) -> AsyncIterator[httpx.AsyncClient]:
        async with self.http() as http:
            r = await http.post("/login", json={"user": user, "password": password})
            assert r.status_code == 200, r.text
            yield http

    async def device(self, browser: httpx.AsyncClient, name: str = "laptop") -> Credentials:
        code = (await browser.post("/api/pairing-codes", json={})).json()["code"]
        async with self.http() as http:
            return await pair(BASE, code, name, http=http)

    async def run_jobs(self, user: str = USER) -> int:
        ran = 0
        while await self.mailbox.engine(user).run_once():
            ran += 1
        return ran


def _server(name: str, clock: Clock | None = None) -> Server:
    server = Server(_TMP / f"{name}.db", clock or Clock())
    server.mailbox.set_user(USER, PASSWORD)
    return server


def _reflection(date: str) -> dict:
    doc = _sensitive_doc()
    doc["date"] = date
    return doc


def _db_bytes(db: Path) -> bytes:
    return b"".join(p.read_bytes() for p in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm"))
                    if p.exists())


def _log_lines() -> int:
    try:
        return len(egress.log_path().read_text().splitlines())
    except FileNotFoundError:
        return 0


async def _upsert(doc: dict) -> None:
    await get_store().upsert_reflection(doc)


# ── login ─────────────────────────────────────────────────────────────────────

async def check_login():
    server = _server("login")
    async with server.http() as http:
        r = await http.get("/")
        assert r.status_code == 303 and r.headers["location"] == "/login", r
        assert (await http.get("/api/logs")).status_code == 401
        assert (await http.post("/login", json={"user": USER, "password": "wrong password"})).status_code == 401
        assert (await http.post("/login", content=f"user={USER}&password={PASSWORD}",
                                headers={"Content-Type": "application/x-www-form-urlencoded"})).status_code == 415
        r = await http.post("/login", json={"user": USER, "password": PASSWORD})
        assert r.status_code == 200 and "httponly" in r.headers["set-cookie"].lower(), r.headers
        assert (await http.get("/")).status_code == 200
        assert (await http.get("/api/logs")).json() == []
        # State changes are JSON-only: a cross-site form post is refused.
        r = await http.post("/api/trigger", content="date=2026-10-19",
                            headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert r.status_code == 415
        await http.post("/logout", json={})
        assert (await http.get("/api/logs")).status_code == 401
    server.mailbox.set_user("guest", "another long password")
    async with server.http() as http:
        codes = [(await http.post("/login", json={"user": "guest", "password": "nope"})).status_code
                 for _ in range(6)]
        assert codes == [401] * 5 + [429], codes
        assert (await http.post("/login", json={"user": "guest", "password": "another long password"})).status_code == 429
    for asset in ("/manifest.webmanifest", "/sw.js", "/icon-192.png", "/login"):
        async with server.http() as http:
            assert (await http.get(asset)).status_code == 200, asset
    print("✓ cloud: dashboard and API need a login; logins throttle; JSON-only writes")


# ── pairing and revocation ────────────────────────────────────────────────────

async def check_pairing():
    clock = Clock()
    server = _server("pairing", clock)
    async with server.browser() as browser:
        code = (await browser.post("/api/pairing-codes", json={})).json()["code"]
        async with server.http() as http:
            creds = await pair(BASE, code.lower(), "work laptop", http=http)  # codes are case-insensitive
            assert creds.user_id == USER and creds.url == BASE and creds.token.startswith("wsd_")
            try:
                await pair(BASE, code, "again", http=http)
                raise AssertionError("a pairing code must work once")
            except Refused:
                pass
            stale = (await browser.post("/api/pairing-codes", json={})).json()["code"]
            clock.now += timedelta(minutes=11)
            try:
                await pair(BASE, stale, "late", http=http)
                raise AssertionError("an expired pairing code must be refused")
            except Refused:
                pass
            async with CloudClient(creds, http) as client:
                assert (await client.poll(0, wait=0)) == {"job": None, "decisions": [], "cursor": 0}
        devices = (await browser.get("/api/devices")).json()
        assert [d["name"] for d in devices] == ["work laptop"] and devices[0]["revoked_at"] is None
    raw = _db_bytes(server.db)
    assert creds.token.encode() not in raw and code.replace("-", "").encode() not in raw
    from wisen.cloud.auth import token_hash
    assert token_hash(creds.token).encode() in raw
    try:
        await pair("http://wisen.example.org", code, "x")
        raise AssertionError("pairing over plain http to a remote host must be refused")
    except ValueError:
        pass
    print("✓ cloud: pairing spends a one-time code on a device token; only its hash is stored")


async def check_revoke():
    clock = Clock()
    server = _server("revoke", clock)
    async with server.browser() as browser:
        creds = await server.device(browser)
        await browser.post("/api/trigger", json={"date": "2026-10-19"})
        async with server.http() as http, CloudClient(creds, http) as client:
            job = (await client.poll(0, wait=0))["job"]
            assert job and job["kind"] == "synthesize" and job["args"] == {"date": "2026-10-19"}, job
            r = await browser.post(f"/api/devices/{creds.device_id}/revoke", json={})
            assert r.status_code == 200, r.text
            for call in (client.poll(0, wait=0), client.heartbeat(job["id"]),
                         client.send_packet(result_packet(_reflection("2026-10-19"), user=USER,
                                                          device_id=creds.device_id))):
                try:
                    await call
                    raise AssertionError("a revoked token must get 401")
                except Revoked:
                    pass
            r = await http.post("/api/device/poll", json={}, headers={"Authorization": f"Bearer {creds.token}"})
            assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
        # Its job went straight back to the queue for another device.
        other = await server.device(browser, "desktop")
        async with server.http() as http, CloudClient(other, http) as client:
            assert (await client.poll(0, wait=0))["job"]["id"] == job["id"]
    print("✓ cloud: a revoked device token gets 401 and its leased job is released")


# ── results and push ──────────────────────────────────────────────────────────

async def check_result_sync_and_push():
    server = _server("results")
    store = get_store()
    async with server.browser() as browser:
        await browser.post("/api/push/subscriptions", json={"subscription": {
            "endpoint": "https://push.example/phone", "keys": {"p256dh": "k", "auth": "a"}}})
        await browser.post("/api/push/subscriptions", json={"subscription": {
            "endpoint": "https://push.example/old-browser", "keys": {"p256dh": "k", "auth": "a"}}})
        server.sender.gone.add("https://push.example/old-browser")
        creds = await server.device(browser)
        await _upsert(_reflection("2026-10-18"))
        async with server.http() as http:
            await worker.run(creds, store, http=http, once=True)
            logs = (await browser.get("/api/logs")).json()
            assert [log["date"] for log in logs] == ["2026-10-18"] and logs[0]["device_id"] == creds.device_id
            assert FAKE_KEY not in json.dumps(logs) and "<secret>" in logs[0]["insight"]
            assert await server.run_jobs() == 1
            assert server.sender.sent == [("https://push.example/phone", {
                "title": "Reflection ready", "body": "2026-10-18 · mild-friction", "url": "/#day-2026-10-18"})]
            # A dead subscription is dropped after its first failed send.
            assert [s["subscription"]["endpoint"] for s in server.mailbox._subscriptions(USER)] == [
                "https://push.example/phone"]
            # Nothing changed: nothing is sent again, and nothing is pushed.
            sent = _log_lines()
            await worker.run(creds, store, http=http, once=True)
            assert _log_lines() == sent and await server.run_jobs() == 0
            # A changed reflection is sent once more and pushed once more.
            doc = _reflection("2026-10-18")
            doc["insight"] = "A new insight."
            await _upsert(doc)
            await worker.run(creds, store, http=http, once=True)
            assert _log_lines() == sent + 1 and await server.run_jobs() == 1 and len(server.sender.sent) == 2
            assert (await browser.get("/api/logs")).json()[0]["insight"] == "A new insight."
    entry = json.loads(egress.log_path().read_text().splitlines()[-1])
    assert entry["destination"] == "cloud:localhost" and entry["packet_type"] == "result"
    print("✓ cloud: a day's reflection syncs laptop -> server once and triggers one push")


# ── approvals ─────────────────────────────────────────────────────────────────

DIFF = "--- a/CLAUDE.md\n+++ b/CLAUDE.md\n@@ -1 +1,2 @@\n # Project\n+Run tests with python3 -m tests.<name>.\n"


async def check_approvals():
    server = _server("approvals")
    store = get_store()
    local = Proposals(store)
    proposal = await local.create("wisen:CLAUDE.md", {
        "diff": DIFF, "rationale": "Agents keep asking how to run tests.",
        "evidence": ["how do I run the tests?"], "conversation": "CANARY_PAUSED_RUN",
    }, request_id="gardener:1", actor="gardener", expires_at=datetime.now(UTC) + timedelta(days=3))
    async with server.browser() as browser:
        await browser.post("/api/push/subscriptions", json={"subscription": {
            "endpoint": "https://push.example/phone", "keys": {"p256dh": "k", "auth": "a"}}})
        creds = await server.device(browser)
        async with server.http() as http:
            await worker.run(creds, store, http=http, once=True)
            (inbox,) = (await browser.get("/api/proposals")).json()
            assert inbox["id"] == proposal.id and inbox["state"] == "proposed" and inbox["target"] == "wisen:CLAUDE.md"
            assert inbox["payload"]["diff"] == DIFF and "CANARY_PAUSED_RUN" not in json.dumps(inbox)
            await server.run_jobs()
            # Not part of a daily batch, so it is pushed on its own.
            assert {"title": "Proposal waiting", "body": "wisen:CLAUDE.md", "url": f"/#proposal-{proposal.id}"} in [
                message for _, message in server.sender.sent], server.sender.sent
            # The first decision wins; a second tap (another device, a retry) gets it back.
            url = f"/api/proposals/{proposal.id}/decision"
            first = (await browser.post(url, json={"decision": "approved", "request_id": "tap-0001"})).json()
            again = (await browser.post(url, json={"decision": "approved", "request_id": "tap-0001"})).json()
            other = (await browser.post(url, json={"decision": "rejected", "request_id": "tap-0002",
                                                   "note": "no"})).json()
            assert first["decision"] == "approved" and not first["already_decided"], first
            assert again == first and other["decision"] == "approved" and other["already_decided"], other
            # The laptop hears about it on its next poll and records the same decision.
            # The approval then reaches the applier, which refuses it here: no
            # project called "wisen" exists on this laptop (tests/test_gardener.py
            # covers the branch itself).
            await worker.run(creds, store, http=http, once=True)
            moves = ["gardener:1", "tap-0001", f"failed:{proposal.id}"]
            assert (await local.get(proposal.id)).state == "failed"
            assert [e.request_id for e in await local.history(proposal.id)] == moves
            # The feed is replayed from the start after a lost cursor: still one decision.
            await worker.apply_decisions(store, (await _poll(creds, http, 0))["decisions"])
            assert [e.request_id for e in await local.history(proposal.id)] == moves
            # A proposal never changes once sent.
            changed = proposal_packet(proposal.id, "wisen:CLAUDE.md", {**proposal.payload, "rationale": "other"},
                                      proposal.expires_at, user=USER, device_id=creds.device_id)
            async with CloudClient(creds, http) as client:
                try:
                    await client.send_packet(changed)
                    raise AssertionError("a changed proposal must be refused")
                except Refused as e:
                    assert "409" in str(e)
    print("✓ cloud: a proposal reaches the inbox; the first decision wins and reaches the laptop")


async def _poll(creds: Credentials, http: httpx.AsyncClient, cursor: int) -> dict:
    async with CloudClient(creds, http) as client:
        return await client.poll(cursor, wait=0)


# ── restart mid-lease ─────────────────────────────────────────────────────────

async def check_restart_mid_lease():
    clock = Clock()
    server = _server("restart", clock)
    async with server.browser() as browser:
        laptop = await server.device(browser, "laptop")
        desktop = await server.device(browser, "desktop")
        await browser.post("/api/trigger", json={"date": "2026-10-19"})
    async with server.http() as http:
        job = (await _poll(laptop, http, 0))["job"]
    # The server process restarts while the laptop holds the lease. The lease
    # is in the database, so the laptop's heartbeat still holds it.
    server = Server(server.db, clock, server.sender)
    async with server.http() as http, CloudClient(laptop, http) as client:
        await client.heartbeat(job["id"])
        clock.now += LEASE / 2
        assert (await _poll(desktop, http, 0))["job"] is None  # still leased to the laptop
        # The laptop goes quiet; once its lease expires the job is recovered.
        clock.now += LEASE
        again = (await _poll(desktop, http, 0))["job"]
        assert again["id"] == job["id"] and again["attempts"] == 2, again
        # The laptop wakes up late: its lease was fenced off, so its result is refused.
        status = {"job_id": job["id"], "kind": "synthesize", "state": "succeeded",
                  "started_at": T0.isoformat(), "finished_at": T0.isoformat()}
        try:
            await client.finish(job["id"], job_status_packet(status, user=USER, device_id=laptop.device_id))
            raise AssertionError("a finish from a lost lease must be refused")
        except Refused:
            pass
    async with server.http() as http, CloudClient(desktop, http) as client:
        await client.finish(job["id"], job_status_packet(status, user=USER, device_id=desktop.device_id))
    done = await server.mailbox.engine(USER).get(job["id"])
    assert done.state == "succeeded" and done.result["device_id"] == desktop.device_id, done
    print("✓ cloud: a server restart mid-lease keeps the lease; an expired one is recovered and fenced")


# ── schedule ──────────────────────────────────────────────────────────────────

async def check_schedule():
    clock = Clock()
    clock.now = datetime(2026, 10, 20, 4, 0, tzinfo=UTC)
    server = Server(_TMP / "schedule.db", clock)
    server.mailbox.set_user(USER, PASSWORD, timezone="Europe/Berlin", daily_at="07:00")  # 05:00 UTC in October
    for _ in range(2):  # every start ensures the timer; the second is a no-op
        await server.mailbox.ensure_schedule(USER)
    assert await server.run_jobs() == 0  # not due before 07:00 Berlin
    jobs = lambda: [(j.kind, j.args, j.state) for j in server.mailbox.device_jobs(USER, ("queued",))]
    clock.now = datetime(2026, 10, 20, 5, 1, tzinfo=UTC)
    assert await server.run_jobs() == 1
    assert jobs() == [("synthesize", {"date": "2026-10-19"}, "queued")], jobs()
    # The wake repeats (at-least-once): the handler changes nothing the second time.
    with sqlite3.connect(server.db) as conn:
        conn.execute("UPDATE jobs SET state = 'queued' WHERE kind = 'schedule-daily' AND args LIKE '%10-20%'")
    assert await server.run_jobs() == 1 and jobs() == [("synthesize", {"date": "2026-10-19"}, "queued")]
    with sqlite3.connect(server.db) as conn:
        pending = conn.execute("SELECT args, run_after FROM jobs WHERE kind = 'schedule-daily' "
                               "AND state = 'queued'").fetchall()
    assert pending == [('{"date": "2026-10-21"}', "2026-10-21T05:00:00.000000+00:00")], pending
    async with server.browser() as browser:
        assert (await browser.get("/api/queue")).json() == ["2026-10-19"]
    print("✓ cloud: the daily schedule is a stored timer that queues yesterday for a device, once")


# ── backup and restore ────────────────────────────────────────────────────────

async def check_backup_restore():
    server = _server("backup")
    async with server.browser() as browser:
        creds = await server.device(browser)
        async with server.http() as http, CloudClient(creds, http) as client:
            for date in ("2026-10-16", "2026-10-17"):
                await client.send_packet(result_packet(_reflection(date), user=USER, device_id=creds.device_id))
        before = {path: (await browser.get(path)).json() for path in ("/api/logs", "/api/devices", "/api/proposals")}
    snapshot = backup(server.db, _TMP / "backups", keep=2)
    for _ in range(2):
        backup(server.db, _TMP / "backups", keep=2)
    assert len(list((_TMP / "backups").glob("wisen-*.db"))) <= 2
    # A running server, even an idle one, is never restored over.
    async with server.app.router.lifespan_context(server.app):
        try:
            restore(snapshot, server.db)
            raise AssertionError("restore over a running server must be refused")
        except RuntimeError:
            pass
    # Disaster: the file is gone. Restore the snapshot and start the server again.
    for p in server.db.parent.glob(server.db.name + "*"):
        p.unlink()
    restore(snapshot, server.db)
    restarted = Server(server.db, Clock())
    async with restarted.http() as http:
        await http.post("/login", json={"user": USER, "password": PASSWORD})
        after = {path: (await http.get(path)).json() for path in before}
        assert after == before and len(after["/api/logs"]) == 2
        # The device token survives the restore too.
        assert (await _poll(creds, http, 0))["job"] is None
    print("✓ cloud: a consistent snapshot restores to the same data, and never under a running server")


# ── the server's packet check ─────────────────────────────────────────────────

async def check_rejections():
    server = _server("reject")
    async with server.browser() as browser:
        creds = await server.device(browser)
    good = result_packet(_reflection("2026-10-17"), user=USER, device_id=creds.device_id)

    def readdressed(packet: dict, **changes) -> dict:
        packet = {**packet, **changes}
        return {**packet, "packet_id": content_id(packet)}

    unredacted = readdressed(good, insight=f"key {FAKE_KEY}")
    cases = {
        "extra field": ({**good, "raw_transcript": "CANARY_REJECTED"}, 422),
        "unknown type": ({**good, "packet_type": "transcript"}, 422),
        "stage B observation": (readdressed(good, packet_type="observation"), 422),
        "unredacted secret": (unredacted, 422),
        "over its cap": (readdressed(good, synthesis_context="a" * 5000), 422),
        "wrong packet_id": ({**good, "insight": "tampered"}, 422),
        "another user": (readdressed(good, user_id="mallory"), 409),
        "another device": (readdressed(good, device_id="0" * 32), 409),
    }
    async with server.http() as http:
        for name, (packet, status) in cases.items():
            r = await http.post("/api/device/packets", content=json.dumps(packet),
                                headers={"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"})
            assert r.status_code == status, (name, r.status_code, r.text)
            assert "CANARY_REJECTED" not in r.text and FAKE_KEY not in r.text, (name, r.text)
        r = await http.post("/api/device/packets", content=json.dumps(good),
                            headers={"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"})
        assert r.json() == {"stored": True, "packet_id": good["packet_id"]}, r.text
    # A diff is never cut or rewritten: the laptop's gate refuses it whole.
    for diff in (DIFF + f"+export KEY={FAKE_KEY}\n", DIFF + "+" + "x" * 20_000 + "\n"):
        try:
            proposal_packet("a" * 32, "wisen:CLAUDE.md", {"diff": diff, "rationale": "r"}, None)
            raise AssertionError("an unsafe diff must be rejected, not rewritten")
        except egress.EgressRejected as e:
            assert FAKE_KEY not in str(e)
    assert server.mailbox.packets(USER, "result")[0]["date"] == "2026-10-17"
    print("✓ cloud: the server rejects off-schema, unredacted, over-cap and mis-addressed packets")


async def check_body_limit():
    server = _server("body-limit")

    async def chunks():
        for _ in range(3):
            yield b" " * 500_000

    async with server.http() as http:
        for path in ("/login", "/api/device/pair"):
            r = await http.post(path, content=chunks(), headers={"Content-Type": "application/json"})
            assert "content-length" not in r.request.headers, r.request.headers
            assert r.status_code == 413, (path, r.status_code, r.text)
        r = await http.post("/login", content=b" " * 1_500_000, headers={"Content-Type": "application/json"})
        assert r.status_code == 413, r.text
        r = await http.post("/login", json={"user": USER, "password": PASSWORD})
        assert r.status_code == 200, r.text
    print("✓ cloud: an oversized body gets 413, chunked or not")


# ── no model key ──────────────────────────────────────────────────────────────

MODEL_ENV = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "DEEPSEEK_API_KEY",
             "WISEN_MODEL", "WISEN_SYNTH_MODEL")


# Runs in a fresh interpreter with no model settings: a full server flow
# importing only what `wisen cloud serve` imports, then proof that no model
# library was ever loaded. argv: the database path, a reflection JSON file.
SERVER_CHILD = """
import asyncio, json, os, sys
from pathlib import Path
import httpx
db, doc = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text())
os.environ["WISEN_CLOUD_DB"] = str(db)
from wisen.cloud.client import CloudClient, pair
from wisen.cloud.mailbox import Mailbox
from wisen.cloud.server import from_env
from wisen.packets import result_packet

async def flow():
    Mailbox(db, sender=None).set_user("vela", "correct horse battery")
    app = from_env(run_engines=False)
    base = "http://localhost"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base) as http:
        r = await http.post("/login", json={"user": "vela", "password": "correct horse battery"})
        assert r.status_code == 200, r.text
        code = (await http.post("/api/pairing-codes", json={})).json()["code"]
        creds = await pair(base, code, "laptop", http=http)
        await http.post("/api/trigger", json={"date": "2026-10-19"})
        async with CloudClient(creds, http) as client:
            await client.send_packet(result_packet(doc, user="vela", device_id=creds.device_id))
            assert (await client.poll(0, wait=0))["job"]["kind"] == "synthesize"
        health = (await http.get("/health")).json()
    assert health["model_key"] is False, health
    assert await Mailbox(db, sender=None).engine("vela").run_once()  # the push job

asyncio.run(flow())
loaded = [m for m in ("pydantic_ai", "wisen.llm", "wisen.synthesis.agent", "openai", "anthropic", "google.genai")
          if m in sys.modules]
assert not loaded, f"the server imported model code: {loaded}"
"""


async def check_no_model_key():
    env = {k: v for k, v in os.environ.items() if k not in MODEL_ENV}
    doc = _TMP / "nokey-reflection.json"
    doc.write_text(json.dumps(_reflection("2026-10-18")))
    child = await asyncio.to_thread(
        subprocess.run, [sys.executable, "-c", SERVER_CHILD, str(_TMP / "nokey.db"), str(doc)],
        env=env, cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True, check=False,
    )
    assert child.returncode == 0, child.stdout + child.stderr
    deploy = Path(__file__).resolve().parent.parent / "deploy"
    for sample in ("wisen.env.example", "wisen.service"):
        for line in (deploy / sample).read_text().splitlines():
            setting = line.split("=", 1)[0].strip().removeprefix("Environment=")
            assert line.lstrip().startswith("#") or setting not in MODEL_ENV, f"{sample}: {line}"
    print("✓ cloud: the server runs a full flow with no model key and imports no model code")


# ── the privacy line, end to end ──────────────────────────────────────────────

MOOD = {"label": "iterating", "summary": "small loops", "evidence": "a quote"}
QUESTION = {"question": "why?", "evidence": "a quote"}


def _fake_model(prompts: list[str]) -> None:
    """The Observer 'echoes' the raw turns it read into its notes (the worst
    case); the Synthesizer writes a canned reflection."""
    async def generate(system_prompt, user_prompt, output_type, model):
        prompts.append(user_prompt)
        if output_type is DayObservation:
            return DayObservation(
                mood_signals_user=user_prompt, mood_signals_agent="careful", topics_raw=RAW,
                key_moments=[{"session_source": "claude-code", "quote": q, "significance": "raw"} for q in RAW],
                commit_drift="none", config_changes_observed=[], friction_observed=RAW,
                yesterday_recs_followed=[], yesterday_recs_ignored=[], model_usage_observed=[],
                day_summary=user_prompt,
            )
        return Reflection(
            date="", mind_map_nodes=[{"topic": "pagination", "weight": 0.8, "is_new": True}], mind_map_edges=[],
            your_mood=MOOD, your_question=QUESTION, agent_mood=MOOD, agent_question=QUESTION,
            alignment_score=0.8, alignment_label="aligned", friction_points=[], recommendations=[],
            config_diffs=[], friction_deltas=[], model_usage=[], acted_on_recommendations=[],
            seven_day_pattern=[], synthesis_context="A calm day of API work.",
        )

    agent._generate = generate
    synthesis_job.get_model = lambda: "test:model"


async def check_no_marker_reaches_server():
    server = _server("markers")
    store = get_store()
    prompts: list[str] = []
    _fake_model(prompts)
    # Local data with every kind of canary: off-allow-list fields, secrets,
    # a paused agent run inside a proposal.
    await _upsert({**_poisoned_doc(), "date": "2026-10-17"})
    await _upsert({**_sensitive_doc(), "date": "2026-10-16"})
    await Proposals(store).create("wisen:AGENTS.md", {
        "diff": DIFF, "rationale": f"see {ABS_PATH}", "evidence": [f"mail {EMAIL}"],
        "conversation": "CANARY_PAUSED_RUN",
    }, request_id="gardener:markers", actor="gardener")
    async with server.browser() as browser:
        creds = await server.device(browser)
        # The server asks for the fixture day; the laptop synthesizes it from
        # the raw fixture sessions and reports back.
        await browser.post("/api/trigger", json={"date": FIXTURE_DATE})
        async with server.http() as http:
            await worker.run(creds, store, http=http, once=True)
            await worker.run(creds, store, http=http, once=True)
        assert prompts and all(raw in prompts[0] for raw in RAW), "the fake Observer never saw the raw fixtures"
        dates = [log["date"] for log in (await browser.get("/api/logs")).json()]
        assert {FIXTURE_DATE, "2026-10-17", "2026-10-16"} <= set(dates), dates
        assert [p["target"] for p in (await browser.get("/api/proposals")).json()] == ["wisen:AGENTS.md"]
    await server.run_jobs()
    (job,) = server.mailbox.device_jobs(USER, ("succeeded",))
    assert job.args == {"date": FIXTURE_DATE} and job.result["state"] == "succeeded", job
    raw = _db_bytes(server.db).decode("utf-8", errors="replace")
    assert "A calm day of API work." in raw  # the scan does see what was uploaded
    markers = [*MARKERS, *RAW, "CANARY_PAUSED_RUN", FAKE_KEY, ABS_PATH, "/Users/alice", EMAIL, "acme-corp"]
    leaked = [m for m in markers if m in raw]
    assert not leaked, f"in the server database: {leaked}"
    print("✓ cloud: after a full laptop -> server sync of the fixtures, no marker is in the server database")


async def main():
    await check_login()
    await check_pairing()
    await check_revoke()
    await check_result_sync_and_push()
    await check_approvals()
    await check_restart_mid_lease()
    await check_schedule()
    await check_backup_restore()
    await check_rejections()
    await check_body_limit()
    await check_no_model_key()
    await check_no_marker_reaches_server()
    print("\nall cloud tests passed")


if __name__ == "__main__":
    asyncio.run(main())
