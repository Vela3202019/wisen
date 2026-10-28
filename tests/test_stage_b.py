"""
Stage B (docs/plan/cloud-architecture.md §3.2, §12 step 7), end to end in one
process: the server app runs in-process on a temp SQLite file, two laptops
are the real `wisen worker` code on their own temp homes and databases, and
every model is a fake.

  - the packets: a config snapshot is secret-redacted and never cut; the
    server rejects one that is unredacted or off the schema
  - two laptops send their Observer's notes and their editable files, then
    sleep: the morning reflection is made on the server from those packets,
    as ONE reflection for the day, and the Gardener drafts from the snapshot
  - with mock models that echo their prompts, no fixture marker (a secret, a
    path, an email, a file the Gardener may not edit, a field off the packet
    schema) appears in any server-side prompt or row
  - an approved server proposal is applied by the laptop whose file it
    changes, keeping the real text the server only saw redacted
  - a laptop that wakes late with notes makes the day run again; nothing
    new, no second reflection
  - stage A on either side keeps stage-B packets and jobs out
  - a failed run that runs again takes in notes sent since; notes sent while
    the server synthesizes make the run go round again
  - the server's Gardener never drafts a redaction placeholder into a file
  - reflection chat on the server reads reflections only, on DeepSeek

No network or model needed. Run: python3 -m tests.test_stage_b
"""
import asyncio
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

_TMP = Path(tempfile.mkdtemp())
_EMPTY = _TMP / "empty"
_EMPTY.mkdir()
os.environ.update({
    "WISEN_DB_PATH": str(_TMP / "laptop-a.db"),
    "WISEN_USER_ID": "local",
    "WISEN_EGRESS_LOG": str(_TMP / "egress.log"),
    "WISEN_ROOT_DIR": str(_EMPTY),   # the connectors fix their dirs at import; `laptop` repoints them
    "GIT_SCAN_DIRS": str(_EMPTY),
    "WISEN_CLOUD_STAGE": "b",
    "WISEN_MODEL": "test",
})
os.environ.pop("GITHUB_TOKEN", None)

import httpx
from pydantic_ai import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from tests.test_egress import ABS_PATH, EMAIL, FAKE_KEY, MARKERS, _poisoned_doc
from wisen import jobs as jobs_mod
from wisen import store as store_mod
from wisen.chat import agent as chat
from wisen.cloud import worker
from wisen.cloud.client import CloudClient, Credentials, Refused, pair
from wisen.cloud.mailbox import Mailbox
from wisen.cloud.rem import merge_observations
from wisen.cloud.server import create_app
from wisen.connectors import agent_configs, claude_code
from wisen.gardener import loop
from wisen.gardener.applier import apply_redacted
from wisen.gardener.git import Repo, text_diff
from wisen.packets import (
    MAX_SNAPSHOT,
    PacketRejected,
    config_snapshot_packet,
    observation_packet,
    receive,
)
from wisen.proposals import Proposals
from wisen.redact import redact
from wisen.synthesis import agent
from wisen.synthesis.schema import DayObservation, Reflection

USER, PASSWORD = "vela", "correct horse battery"
BASE = "http://localhost"
TODAY = datetime.now(UTC).date()
DAY = (TODAY - timedelta(days=1)).isoformat()          # the day the morning run reflects on
DAY_BEFORE = (TODAY - timedelta(days=2)).isoformat()
# Raw words from the laptops' sessions: the Observer may quote them (a
# positive control that the scans see what crossed).
RAW_A, RAW_B = "add pagination to the items endpoint", "write a csv parser for the import command"
# What must never reach the server: secrets, paths and emails (redacted on the
# way), a file the Gardener may not edit, a file over the snapshot cap,
# fields off the packet schema, a paused agent run.
NEVER = [*MARKERS, FAKE_KEY, ABS_PATH, "/Users/alice", EMAIL, "acme-corp", "CANARY_SETTINGS", "CANARY_OVER_CAP",
         "CANARY_PAUSED_RUN"]
HOME_MD = (f"# Global rules\n- Commit as {EMAIL}.\n- Client repos live in /Users/alice/clients/acme.\n"
           f"- API key for scripts: {FAKE_KEY}\n- Be brief.\n")


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeSender:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, subscription: dict, payload: str) -> None:
        self.sent.append(json.loads(payload))


class Server:
    def __init__(self, name: str, stage: str = "b"):
        self.db = _TMP / f"{name}.db"
        self.clock = Clock(datetime.combine(TODAY, time(6, 0), UTC))
        self.sender = FakeSender()
        self.mailbox = Mailbox(self.db, sender=self.sender, clock=self.clock, stage=stage)
        self.mailbox.set_user(USER, PASSWORD, "UTC", "07:00")
        self.app = create_app(self.mailbox, vapid_public_key="k", secure_cookies=False, run_engines=False)

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=BASE)

    async def browser(self) -> httpx.AsyncClient:
        http = self.http()
        assert (await http.post("/login", json={"user": USER, "password": PASSWORD})).status_code == 200
        return http

    async def device(self, browser: httpx.AsyncClient, name: str) -> Credentials:
        code = (await browser.post("/api/pairing-codes", json={})).json()["code"]
        async with self.http() as http:
            return await pair(BASE, code, name, http=http)

    async def run_jobs(self) -> None:
        while await self.mailbox.engine(USER).run_once():
            pass

    def db_text(self) -> str:
        return b"".join(p.read_bytes() for p in self.db.parent.glob(self.db.name + "*")).decode("utf-8", "replace")


# ── the laptops ───────────────────────────────────────────────────────────────

def _session(home: Path, project: str, day: str, user_turns: list[str]) -> None:
    d = home / ".claude" / "projects" / f"-Users-alice-{project}"
    d.mkdir(parents=True, exist_ok=True)
    lines, at = [], datetime.fromisoformat(f"{day}T10:00:00+00:00")
    for n, text in enumerate(user_turns):
        lines.append({"type": "user", "sessionId": f"{project}-1", "cwd": f"/Users/alice/{project}",
                      "timestamp": (at + timedelta(minutes=2 * n)).isoformat().replace("+00:00", "Z"),
                      "message": {"role": "user", "content": text}})
        lines.append({"type": "assistant", "sessionId": f"{project}-1",
                      "timestamp": (at + timedelta(minutes=2 * n + 1)).isoformat().replace("+00:00", "Z"),
                      "message": {"role": "assistant", "content": [{"type": "text", "text": "Done."}]}})
    (d / f"{project}-1.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def _home(name: str, home_md: str, turns: list[str]) -> Path:
    home = _TMP / name
    (home / ".claude" / "skills" / "big").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text(home_md)
    (home / ".claude" / "settings.json").write_text(json.dumps({"env": {"NOTE": "CANARY_SETTINGS"}}))
    (home / ".claude" / "skills" / "big" / "SKILL.md").write_text("x" * MAX_SNAPSHOT + "\nCANARY_OVER_CAP\n")
    _session(home, name, DAY, turns)
    return home


@contextmanager
def laptop(name: str):
    """Act as one laptop: its home, its database, its home repo."""
    home = _TMP / name
    os.environ.update({"WISEN_DB_PATH": str(_TMP / f"{name}.db"), "WISEN_ROOT_DIR": str(home),
                       "WISEN_GARDENER_HOME_REPO": str(_TMP / f"{name}-home.git")})
    claude_code._CLAUDE_DIR = home / ".claude" / "projects"
    agent_configs._CLAUDE_DIR = home / ".claude"
    store_mod._store = None
    jobs_mod._engine = None
    yield store_mod.get_store()


# ── the fake models ───────────────────────────────────────────────────────────

MOOD = {"label": "iterating", "summary": "small loops", "evidence": "a quote"}
QUESTION = {"question": "why?", "evidence": "a quote"}
laptop_prompts: list[str] = []
server_prompts: list[str] = []


async def generate(system_prompt, user_prompt, output_type, model):
    if output_type is DayObservation:
        # The laptop's Observer, at its worst: it quotes the raw user turns
        # (secrets, paths and emails among them) in its notes.
        laptop_prompts.append(user_prompt)
        turns = [line.removeprefix("You: ") for line in user_prompt.splitlines() if line.startswith("You: ")]
        return DayObservation(
            mood_signals_user=" / ".join(turns), mood_signals_agent="careful", topics_raw=["api"],
            key_moments=[{"session_source": "claude-code", "quote": t, "significance": "raw"} for t in turns],
            commit_drift="none", config_changes_observed=[], friction_observed=["the test command again"],
            yesterday_recs_followed=[], yesterday_recs_ignored=[], model_usage_observed=[],
            day_summary="Asked: " + " / ".join(turns))
    # The server's Synthesizer echoes its whole prompt into the reflection.
    server_prompts.append(user_prompt)
    return Reflection(
        date="", mind_map_nodes=[], mind_map_edges=[], your_mood=MOOD, your_question=QUESTION,
        agent_mood=MOOD, agent_question=QUESTION, alignment_score=0.6, alignment_label="mild-friction",
        friction_points=[], recommendations=[{"target": "claude-md", "action": "Name the test command.",
                                              "reason": "Asked twice."}],
        config_diffs=[], friction_deltas=[], model_usage=[], acted_on_recommendations=[], seven_day_pattern=[],
        synthesis_context=user_prompt)


agent._generate = generate


def _text(messages: list) -> str:
    """Everything a model was sent: prompts, instructions and tool returns."""
    parts = []
    for m in messages:
        if isinstance(m, ModelRequest):
            parts.append(m.instructions or "")
            parts += [str(p.content) for p in m.parts if isinstance(p, SystemPromptPart | UserPromptPart | ToolReturnPart)]
    return "\n".join(parts)


class Gardener:
    """The server's Gardener: reads the home file, proposes one change, and
    finishes once resumed. It keeps what it was sent."""

    def __init__(self):
        self.seen: list[str] = []

    def model(self) -> FunctionModel:
        def respond(messages: list, info: AgentInfo) -> ModelResponse:
            self.seen.append(_text(messages))
            last = messages[-1].parts[-1]
            if isinstance(last, ToolReturnPart) and last.tool_name == "propose_change":
                return ModelResponse(parts=[TextPart("Proposed one change.")])
            if isinstance(last, ToolReturnPart) and last.tool_name == "read_file":
                return ModelResponse(parts=[ToolCallPart("propose_change", {
                    "target": "home:.claude/CLAUDE.md", "old_text": "- Be brief.\n",
                    "new_text": "- Be brief.\n- Name the test command before running tests.\n",
                    "rationale": "Agents keep guessing the test command.",
                    "evidence": [{"date": DAY_BEFORE, "quote": "which test command?"},
                                 {"date": DAY, "quote": "the test command again"}]}, tool_call_id="c1")])
            return ModelResponse(parts=[ToolCallPart("list_files", {}, tool_call_id="l1"),
                                        ToolCallPart("read_file", {"target": "home:.claude/CLAUDE.md"},
                                                     tool_call_id="r1")])
        return FunctionModel(respond)


def _reflection(day: str) -> dict:
    return {"date": day, "mind_map_nodes": [], "mind_map_edges": [], "your_mood": MOOD, "your_question": QUESTION,
            "agent_mood": MOOD, "agent_question": QUESTION, "alignment_score": 0.5, "alignment_label": "friction",
            "friction_points": [], "recommendations": [{"target": "claude-md", "action": "Name the test command.",
                                                        "reason": "which test command?"}],
            "config_diffs": [], "friction_deltas": [], "model_usage": [], "acted_on_recommendations": [],
            "seven_day_pattern": [], "synthesis_context": "Yesterday: which test command?",
            "synthesized_at": f"{DAY}T07:30:00+00:00"}


# ── checks ────────────────────────────────────────────────────────────────────

def check_packets():
    packet = config_snapshot_packet("home:.claude/CLAUDE.md", "a" * 40, HOME_MD, user=USER, device_id="d1")
    assert FAKE_KEY not in packet["text"] and EMAIL not in packet["text"] and "/Users/alice" not in packet["text"]
    assert packet["text"].endswith("- Be brief.\n"), packet["text"]
    assert receive(packet, ("config_snapshot",)).text == packet["text"]
    gone = config_snapshot_packet("home:.claude/CLAUDE.md", None, None, user=USER, device_id="d1")
    assert receive(gone, ("config_snapshot",)).text is None
    for bad in ({**packet, "text": HOME_MD}, {**packet, "path": "/etc/passwd"}, {**packet, "target": "home:~/.zshrc"}):
        try:
            receive(bad, ("config_snapshot",))
            raise AssertionError(f"accepted {sorted(bad)}")
        except PacketRejected:
            pass
    try:
        config_snapshot_packet("home:.claude/CLAUDE.md", None, "x" * (MAX_SNAPSHOT + 1))
        raise AssertionError("an over-cap file was cut instead of refused")
    except ValueError:
        pass
    try:
        receive(packet, ("result", "usage"))   # a stage-A server's list
        raise AssertionError("a stage-A list accepted a snapshot")
    except PacketRejected:
        pass
    print("✓ stage B: a config snapshot is redacted, never cut, and checked again on receipt")


def check_merge_and_apply():
    a = {**{k: "" for k in DayObservation.model_fields}, "topics_raw": ["api", "csv"], "key_moments": [],
         "config_changes_observed": [], "friction_observed": [], "yesterday_recs_followed": [],
         "yesterday_recs_ignored": [], "model_usage_observed": [], "day_summary": "A did the API."}
    b = {**a, "topics_raw": ["csv", "docs"], "day_summary": "B did the parser."}
    merged = merge_observations([("laptop-a", a), ("laptop-b", b)])
    assert merged.topics_raw == ["api", "csv", "docs"], merged.topics_raw
    assert merged.day_summary == "[laptop-a] A did the API.\n\n[laptop-b] B did the parser.", merged.day_summary
    # A change drafted against the redacted text keeps the real lines around it.
    shown = redact(HOME_MD)
    new = shown.replace("- Be brief.\n", "- Be brief.\n- Name it.\n")
    assert apply_redacted(HOME_MD, text_diff("CLAUDE.md", shown, new)) == HOME_MD + "- Name it.\n"
    assert apply_redacted(HOME_MD, text_diff("CLAUDE.md", shown, new.replace("- Be brief.", "- Be short."))) == (
        HOME_MD.replace("- Be brief.", "- Be short.") + "- Name it.\n")
    pem = "a\n-----BEGIN RSA PRIVATE KEY-----\nAAA\n-----END RSA PRIVATE KEY-----\nb\n"
    assert apply_redacted(pem, text_diff("x", redact(pem), redact(pem) + "c\n")) is None   # lines do not map
    assert apply_redacted("plain\n", text_diff("x", "plain\n", "plain\nmore\n")) is None   # nothing redacted
    print("✓ stage B: notes from two laptops merge; a redacted draft applies to the real file")


async def check_reflection_while_asleep():
    server = Server("asleep")
    gardener = Gardener()
    loop.gardener_model = gardener.model
    browser = await server.browser()
    await browser.post("/api/push/subscriptions", json={"subscription": {
        "endpoint": "https://push.example/phone", "keys": {"p256dh": "k", "auth": "a"}}})
    creds_a = await server.device(browser, "laptop-a")
    creds_b = await server.device(browser, "laptop-b")
    # Yesterday's reflection, made by the server the morning before.
    await server.mailbox.store(USER).upsert_reflection(_reflection(DAY_BEFORE))

    _home("laptop-b", "# B\n- Be brief.\n", [RAW_B])
    _home("laptop-a", HOME_MD, [RAW_A, f"use {FAKE_KEY}, mail {EMAIL}, see {ABS_PATH}"])
    async with server.http() as http:
        # Laptop B, then laptop A, send their notes and files, then sleep.
        with laptop("laptop-b") as store:
            async with CloudClient(creds_b, http) as client:
                await worker.dream_up(client, store)
        server.clock.now += timedelta(minutes=5)
        with laptop("laptop-a") as store:
            await store.upsert_reflection({**_poisoned_doc(), "date": DAY_BEFORE})   # stays here in stage B
            await Proposals(store).create("home:.claude/AGENTS.md", {
                "diff": text_diff(".claude/AGENTS.md", None, "rules\n"), "rationale": "x", "evidence": [],
                "conversation": "CANARY_PAUSED_RUN"}, request_id="gardener:local", actor="gardener")
            await worker.run(creds_a, store, http=http, once=True)
    assert len(laptop_prompts) == 2 and RAW_A in laptop_prompts[1], "the laptops' Observers never ran"
    assert len(server.mailbox.observations(USER, DAY)) == 2
    snapshot = server.mailbox.snapshot(USER)
    assert snapshot.list() == ["home:.claude/CLAUDE.md"], snapshot.list()   # the over-cap skill stayed home
    assert snapshot.files["home:.claude/CLAUDE.md"]["device_id"] == creds_a.device_id   # newest upload wins
    assert DAY not in [r["date"] for r in (await browser.get("/api/logs")).json()]   # no laptop result in stage B

    # 07:00: the schedule asks for fresh notes; nobody answers. 07:30: the
    # server reflects on what is already uploaded.
    await server.mailbox.ensure_schedule(USER)
    server.clock.now = datetime.combine(TODAY, time(7, 0), UTC)
    await server.run_jobs()
    assert (await browser.get("/api/queue")).json() == [DAY]
    assert DAY not in [r["date"] for r in (await browser.get("/api/logs")).json()]
    server.clock.now += timedelta(minutes=31)
    await server.run_jobs()
    logs = (await browser.get("/api/logs")).json()
    assert [r["date"] for r in logs] == [DAY], logs
    (job,) = server.mailbox.device_jobs(USER, ("queued",))   # still waiting for a laptop
    assert job.kind == "observe" and job.args["date"] == DAY
    (prompt,) = server_prompts
    assert "[laptop-a]" in prompt and "[laptop-b]" in prompt and RAW_A in prompt and RAW_B in prompt, prompt
    inbox = (await browser.get("/api/proposals")).json()
    drafted = [p for p in inbox if p["target"] == "home:.claude/CLAUDE.md"]
    assert len(drafted) == 1 and drafted[0]["payload"]["device_id"] == creds_a.device_id, inbox
    assert "replay needs the laptop" in drafted[0]["payload"]["eval_summary"]
    assert server.sender.sent[-1] == {"title": "Reflection ready", "body": f"{DAY} · mild-friction · "
                                      f"1 proposal waiting", "url": f"/#day-{DAY}"}, server.sender.sent
    assert any("<email>" in seen for seen in gardener.seen), "the Gardener did not read the redacted file"

    # The privacy line: nothing that must stay home is in any server-side
    # prompt or row, though what the Observer quoted did cross.
    for where, text in (("synthesizer prompt", prompt), ("gardener input", "\n".join(gardener.seen)),
                        ("server database", server.db_text())):
        leaked = [m for m in NEVER if m in text]
        assert not leaked, f"in the {where}: {leaked}"
    assert RAW_A in server.db_text()

    # The owner approves; the server resumes its Gardener run.
    pid = drafted[0]["id"]
    r = await browser.post(f"/api/proposals/{pid}/decision", json={"decision": "approved", "request_id": "web-approve-1"})
    assert r.status_code == 200, r.text
    await server.run_jobs()
    run_state = server.mailbox.store(USER)._connect().execute("SELECT state FROM gardener_runs").fetchone()[0]
    assert run_state == "resumed", run_state

    async with server.http() as http:
        # Laptop B wakes first and answers the observe job; the proposal is
        # not for its files.
        with laptop("laptop-b") as store:
            await worker.run(creds_b, store, http=http, once=True)
            assert await Proposals(store).get(pid) is None
            # The server's reflection of the day before, the Observer's baseline, kept here.
            assert (await store.get_reflection(DAY_BEFORE))["synthesis_context"] == "Yesterday: which test command?"
        # Laptop A wakes and applies the change.
        with laptop("laptop-a") as store:
            await worker.run(creds_a, store, http=http, once=True)
            await worker.run(creds_a, store, http=http, once=True)
            local = await Proposals(store).get(pid)
            assert local is not None and local.state == "applied", local
            repo = Repo(_TMP / "laptop-a", _TMP / "laptop-a-home.git")
            text = repo.read_blob(repo.blob(f"wisen/gardener/{pid}", ".claude/CLAUDE.md"))
            assert text == HOME_MD + "- Name the test command before running tests.\n", text
    await server.run_jobs()
    assert (await server.mailbox.proposals(USER).get(pid)).state == "applied"
    # The late answer ran the day again, which found nothing new: one reflection, one push.
    jobs = {j.request_id: j for j in server.mailbox.device_jobs(USER, ("succeeded",))}
    assert jobs[f"observe:{DAY}"].result["skipped"] == "already observed, no session since", jobs
    assert len(server_prompts) == 1 and [s["title"] for s in server.sender.sent].count("Reflection ready") == 1
    leaked = [m for m in NEVER if m in server.db_text()]
    assert not leaked, f"in the server database: {leaked}"
    await browser.aclose()
    print("✓ stage B: with the laptops asleep the server makes one reflection from both laptops' packets;")
    print("  no marker reaches a server-side prompt or row; the right laptop applies the approved change")


async def check_late_notes():
    """Nobody sent notes before the morning run: it skips, and says why. The
    laptop wakes, answers, and the day runs then."""
    server = Server("late")
    server_prompts.clear()
    browser = await server.browser()
    creds = await server.device(browser, "laptop-c")
    _home("laptop-c", "# C\n", ["fix the flaky login test"])
    await server.mailbox.ensure_schedule(USER)
    server.clock.now = datetime.combine(TODAY, time(7, 0), UTC)
    await server.run_jobs()
    server.clock.now += timedelta(minutes=31)
    await server.run_jobs()
    (synth,) = [j for j in await asyncio.to_thread(_jobs, server, "synthesize")]
    assert synth.result == {"date": DAY, "skipped": "no observations uploaded"}, synth.result
    async with server.http() as http:
        with laptop("laptop-c") as store:
            await worker.run(creds, store, http=http, once=True)
    await server.run_jobs()
    assert [r["date"] for r in (await browser.get("/api/logs")).json()] == [DAY]
    assert len(server_prompts) == 1 and "fix the flaky login test" in server_prompts[0]
    await browser.aclose()
    print("✓ stage B: a day with no notes yet is skipped; notes that arrive later make its reflection")


async def check_failed_run_takes_late_notes():
    """The morning run merges laptop D's notes, then fails (the CLI's login
    expired). Laptop E's notes arrive after; the rerun reflects on both."""
    server = Server("failed")
    server_prompts.clear()
    browser = await server.browser()
    creds_d = await server.device(browser, "laptop-d")
    creds_e = await server.device(browser, "laptop-e")
    _home("laptop-d", "# D\n", ["tidy the release notes"])
    _home("laptop-e", "# E\n", ["profile the slow report query"])
    async with server.http() as http:
        with laptop("laptop-d") as store:
            async with CloudClient(creds_d, http) as client:
                await worker.dream_up(client, store)
    await server.mailbox.ensure_schedule(USER)
    server.clock.now = datetime.combine(TODAY, time(7, 0), UTC)
    await server.run_jobs()
    server.clock.now += timedelta(minutes=31)

    async def expired(system_prompt, user_prompt, output_type, model):
        server_prompts.append(user_prompt)
        raise RuntimeError("Failed to authenticate")

    agent._generate = expired
    try:
        for _ in range(3):
            await server.run_jobs()
            server.clock.now += timedelta(hours=1)
    finally:
        agent._generate = generate
    (synth,) = await asyncio.to_thread(_jobs, server, "synthesize")
    assert synth.state == "failed" and "tidy the release notes" in server_prompts[0], synth
    async with server.http() as http:
        with laptop("laptop-e") as store:
            async with CloudClient(creds_e, http) as client:
                await worker.dream_up(client, store)
    await server.run_jobs()
    (synth,) = await asyncio.to_thread(_jobs, server, "synthesize")
    assert synth.state == "succeeded", synth
    assert "tidy the release notes" in server_prompts[-1] and "profile the slow report query" in server_prompts[-1]
    assert "profile the slow report query" in (await browser.get("/api/logs")).json()[0]["synthesis_context"]
    await browser.aclose()
    print("✓ stage B: a failed run that runs again takes in the notes sent since")


async def check_notes_during_run():
    """Laptop G's notes arrive while the server is synthesizing laptop F's:
    the run goes round again and the day's reflection holds both."""
    server = Server("during")
    server_prompts.clear()
    browser = await server.browser()
    creds_f = await server.device(browser, "laptop-f")
    creds_g = await server.device(browser, "laptop-g")
    _home("laptop-f", "# F\n", ["rename the billing module"])
    _home("laptop-g", "# G\n", ["cache the dashboard query"])
    async with server.http() as http:
        with laptop("laptop-f") as store:
            async with CloudClient(creds_f, http) as client:
                await worker.dream_up(client, store)
    await server.mailbox.ensure_schedule(USER)
    server.clock.now = datetime.combine(TODAY, time(7, 0), UTC)
    await server.run_jobs()
    server.clock.now += timedelta(minutes=31)

    async def slow(system_prompt, user_prompt, output_type, model):
        if output_type is not DayObservation and len(server_prompts) == 0:
            server.clock.now += timedelta(minutes=1)
            async with server.http() as http:
                with laptop("laptop-g") as store:
                    async with CloudClient(creds_g, http) as client:
                        await worker.dream_up(client, store)
        return await generate(system_prompt, user_prompt, output_type, model)

    agent._generate = slow
    try:
        await server.run_jobs()
    finally:
        agent._generate = generate
    (synth,) = await asyncio.to_thread(_jobs, server, "synthesize")
    assert synth.state == "succeeded" and len(server_prompts) == 2, (synth, len(server_prompts))
    assert "rename the billing module" in server_prompts[-1] and "cache the dashboard query" in server_prompts[-1]
    (log,) = (await browser.get("/api/logs")).json()
    assert "cache the dashboard query" in log["synthesis_context"], log
    newest = max(at for _, _, at in server.mailbox.observations(USER, DAY))
    assert (await server.mailbox.store(USER).get_reflection(DAY))["synthesized_at"] == newest
    assert server.mailbox.skip_reason(USER, DAY) is not None
    await browser.aclose()
    print("✓ stage B: notes sent while the server synthesizes make the run go round again")


def check_no_placeholder_written():
    """The server's Gardener sees files redacted: a draft that would write a
    placeholder over the real value is refused."""
    from pydantic_ai import ModelRetry

    from wisen.gardener.agent import Deps, Evidence, _draft
    from wisen.gardener.targets import SnapshotFiles

    shown = redact(HOME_MD)
    evidence = [Evidence(date=DAY_BEFORE, quote="q"), Evidence(date=DAY, quote="q")]

    def draft(new_text: str):
        deps = Deps(snapshot=SnapshotFiles({"home:.claude/CLAUDE.md": {
            "text": shown, "base_blob": "a" * 40, "device_id": "d1"}}), reflection_dates={DAY, DAY_BEFORE})
        return _draft(deps, "c1", "home:.claude/CLAUDE.md", "- Be brief.\n", new_text, "why", evidence)

    for placeholder in ("<secret>", "<email>", "~/", "…/"):
        try:
            draft(f"- Be brief.\n- Run {placeholder}test first.\n")
            raise AssertionError(f"a draft holding {placeholder} was accepted")
        except ModelRetry as e:
            assert placeholder in str(e), e
    assert draft("- Be brief.\n- Run the tests first.\n").proposed == shown + "- Run the tests first.\n"
    print("✓ stage B: the server's Gardener never drafts a redaction placeholder into a file")


def _jobs(server: Server, kind: str) -> list:
    from contextlib import closing

    from wisen.jobs import Job
    with closing(server.mailbox.store(USER)._connect()) as conn:
        return [Job.from_row(r) for r in conn.execute("SELECT * FROM jobs WHERE kind = ?", (kind,))]


async def check_stage_a_keeps_out():
    """A stage-A server takes no stage-B packets; a stage-A laptop answers no
    observe job, and still uploads its reflections."""
    server = Server("stage-a", stage="a")
    browser = await server.browser()
    creds = await server.device(browser, "laptop-d")
    obs = DayObservation(mood_signals_user="", mood_signals_agent="", topics_raw=[], key_moments=[],
                         commit_drift="", config_changes_observed=[], friction_observed=[],
                         yesterday_recs_followed=[], yesterday_recs_ignored=[], model_usage_observed=[],
                         day_summary="")
    async with server.http() as http, CloudClient(creds, http) as client:
        try:
            await client.send_packet(observation_packet(obs, DAY, user=USER, device_id=creds.device_id))
            raise AssertionError("a stage-A server stored an observation")
        except Refused as e:
            assert "422" in str(e), e
    stage_b = Server("b-for-a-laptop")
    browser_b = await stage_b.browser()
    creds_b = await stage_b.device(browser_b, "laptop-e")
    await browser_b.post("/api/trigger", json={"date": DAY})
    os.environ["WISEN_CLOUD_STAGE"] = "a"
    try:
        async with stage_b.http() as http:
            with laptop("laptop-e") as store:
                (_TMP / "laptop-e").mkdir(exist_ok=True)
                await store.upsert_reflection(_reflection(DAY_BEFORE))
                laptop_prompts.clear()
                await worker.run(creds_b, store, http=http, once=True)
        assert not laptop_prompts and not stage_b.mailbox.observations(USER, DAY)
        (job,) = stage_b.mailbox.device_jobs(USER, ("queued",))   # failed once, retried later
        assert job.kind == "observe" and "StageBOff" in job.error, job
        assert [r["date"] for r in (await browser_b.get("/api/logs")).json()] == [DAY_BEFORE]
    finally:
        os.environ["WISEN_CLOUD_STAGE"] = "b"
    await browser.aclose()
    await browser_b.aclose()
    print("✓ stage B: a stage-A server refuses the new packets; a stage-A laptop sends no notes")


async def check_chat():
    """Reflection chat on the server: the reflection tools on the server's
    store, never the raw-history search."""
    server = Server("chat")
    store = server.mailbox.store(USER)
    await store.upsert_reflection(_reflection(DAY_BEFORE))
    seen: list[str] = []

    async def respond(messages: list, info: AgentInfo):
        seen.append(_text(messages))
        assert {t.name for t in info.function_tools} == {"get_reflection", "list_reflections"}, info.function_tools
        if isinstance(messages[-1].parts[-1], ToolReturnPart):
            yield f"You were told: {seen[-1][-300:]}"
        else:
            yield {0: DeltaToolCall("get_reflection", json.dumps({"date": DAY_BEFORE}), tool_call_id="g1")}

    events = []
    os.environ.update({"WISEN_CHAT_MODEL": "deepseek-v4-flash", "DEEPSEEK_API_KEY": "fake-deepseek-key"})
    with chat._get_agent(raw_history=False).override(model=FunctionModel(stream_function=respond)):
        async for event in chat.stream_chat([{"role": "user", "content": "how was it?"}], store=store,
                                            raw_history=False):
            events.append(event)
    answer = "".join(e["data"] for e in events if e["type"] == "token")
    assert "which test command?" in answer, events
    assert "raw transcripts and config files stay on their laptop" in seen[0]

    # The same over HTTP: POST /api/chat, logged in, stage B only, nothing stored.
    ask = {"messages": [{"role": "user", "content": "how was it?"}]}
    async with server.http() as http:
        assert (await http.post("/api/chat", json=ask)).status_code == 401
    browser = await server.browser()
    for bad in ({"messages": []}, {"messages": [{"role": "user", "content": "x"}] * 51},
                {"messages": [{"role": "user", "content": "x" * 8001}]}, {**ask, "session_id": "s"},
                {"messages": [{"role": "assistant", "content": "x"}]}):
        assert (await browser.post("/api/chat", json=bad)).status_code == 422, bad
    with chat._get_agent(raw_history=False).override(model=FunctionModel(stream_function=respond)):
        r = await browser.post("/api/chat", json={**ask, "context_date": DAY_BEFORE})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream"), r
    lines = [json.loads(line.removeprefix("data: ")) for line in r.text.splitlines() if line.startswith("data: ")]
    assert lines[-1] == {"done": True} and "which test command?" in "".join(
        e["data"] for e in lines if e.get("type") == "token"), lines
    with server.mailbox.store(USER)._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0] == 0
    await browser.aclose()
    stage_a = Server("chat-stage-a", stage="a")
    browser = await stage_a.browser()
    assert (await browser.post("/api/chat", json=ask)).status_code == 404
    await browser.aclose()
    print("✓ stage B: reflection chat answers from the server's reflections only, over POST /api/chat")


def check_chat_model():
    """The server's chat runs on DeepSeek, named by WISEN_CHAT_MODEL, with
    DEEPSEEK_API_KEY; neither has a default. The laptop's chat keeps WISEN_MODEL."""
    from pydantic_ai.models.openai import OpenAIChatModel

    saved = {k: os.environ.get(k) for k in ("WISEN_CHAT_MODEL", "DEEPSEEK_API_KEY")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        for env, missing in (({}, "WISEN_CHAT_MODEL"), ({"WISEN_CHAT_MODEL": "deepseek-v4-flash"}, "DEEPSEEK_API_KEY")):
            os.environ.update(env)
            try:
                chat._make_agent(raw_history=False)
                raise AssertionError(f"the server's chat built without {missing}")
            except RuntimeError as e:
                assert missing in str(e), e
        os.environ["DEEPSEEK_API_KEY"] = "fake-deepseek-key"
        model = chat._make_agent(raw_history=False).model
        assert isinstance(model, OpenAIChatModel) and model.system == "deepseek", model
        assert model.model_name == "deepseek-v4-flash" and "deepseek.com" in model.base_url, model.base_url
        assert getattr(chat._make_agent(raw_history=True).model, "system", None) != "deepseek"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("✓ stage B: the server's chat runs on DeepSeek by WISEN_CHAT_MODEL and DEEPSEEK_API_KEY")


def check_server_synthesis_path():
    """A stage-B server synthesizes on `claude -p` unless told otherwise; the
    API model is the fallback by config."""
    from wisen.cloud.server import from_env

    saved = {k: os.environ.get(k) for k in ("WISEN_CLOUD_STAGE", "WISEN_SYNTH_MODEL", "WISEN_CLOUD_DB")}
    try:
        os.environ.update({"WISEN_CLOUD_STAGE": "b", "WISEN_CLOUD_DB": str(_TMP / "from-env.db")})
        os.environ.pop("WISEN_SYNTH_MODEL", None)
        from_env(run_engines=False)
        assert os.environ["WISEN_SYNTH_MODEL"] == "claude-cli:sonnet"
        os.environ["WISEN_SYNTH_MODEL"] = "api"
        from_env(run_engines=False)
        assert os.environ["WISEN_SYNTH_MODEL"] == "api"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("✓ stage B: the server synthesizes on claude -p by default; WISEN_SYNTH_MODEL=api falls back")


async def main():
    check_packets()
    check_merge_and_apply()
    await check_reflection_while_asleep()
    await check_late_notes()
    await check_failed_run_takes_late_notes()
    await check_notes_during_run()
    check_no_placeholder_written()
    await check_stage_a_keeps_out()
    await check_chat()
    check_chat_model()
    check_server_synthesis_path()
    print("\nall stage B tests passed")


if __name__ == "__main__":
    asyncio.run(main())
