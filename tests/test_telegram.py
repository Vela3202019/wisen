"""
Telegram (cloud-architecture §9.2, step 6), against a fake Bot API: the
server app and mailbox run in-process as in tests/test_cloud.py, the bot's
HTTP calls go to a fake Telegram, and the laptop's chat model is a
FunctionModel. Covers:

  - linking: the dashboard's one-time code, sent as /link in a private chat
  - an unlinked chat is ignored: no reply, no question, no decision
  - a proposal message carries its target, a capped rationale and a
    dashboard link, never the diff or the evidence quotes
  - approving in Telegram and then in the web app records one decision; a
    retried tap returns the first result; a tap after the web app decided
    says so
  - /search while the laptop sleeps is answered after it wakes; a plain
    question is answered from reflections only
  - the long-poll loop confirms each update and survives a failed call or
    an update that raises
  - the bot token never reaches a log line, an error, or the database

No network or model needed. Run: python3 -m tests.test_telegram
"""
import asyncio
import io
import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx

from tests.test_cloud import (
    _TMP,
    BASE,
    DIFF,
    USER,
    Clock,
    FakeSender,
    Server,
    _db_bytes,
    _reflection,
    _upsert,
)
from wisen.cloud import worker
from wisen.cloud.mailbox import Mailbox
from wisen.cloud.server import create_app
from wisen.cloud.telegram import (
    DESCRIPTION_CAP,
    HELP,
    TelegramApi,
    TelegramBot,
    TelegramError,
)
from wisen.proposals import Proposals
from wisen.store import get_store

TOKEN = "123456789:AAH-telegram-test-token-0123456789abc"
PUBLIC_URL = "https://wisen.example.org"
CHAT, STRANGER = 4242, 9999
EVIDENCE = "EVIDENCE_MARKER: the user said this exact thing"
RATIONALE = "Agents keep asking how to run tests. " * 20  # longer than the cap


class FakeTelegram:
    """The Bot API, as far as the bot uses it. Every request is kept, so a
    test can check everything that would have reached Telegram."""

    def __init__(self):
        self.requests: list[tuple[str, dict]] = []
        self.updates: list[dict] = []
        self.fail: set[str] = set()
        self.polled = asyncio.Event()
        self._message_id = 100

    async def handler(self, request: httpx.Request) -> httpx.Response:
        prefix = f"/bot{TOKEN}/"
        assert request.url.path.startswith(prefix), request.url.path
        method, params = request.url.path.removeprefix(prefix), json.loads(request.content)
        self.requests.append((method, params))
        if method in self.fail:
            return httpx.Response(400, json={"ok": False, "error_code": 400, "description": "Bad Request: nope"})
        result: object = True
        if method == "getUpdates":
            result = [u for u in self.updates if u["update_id"] >= (params.get("offset") or 0)]
            if not result:
                self.polled.set()
                await asyncio.sleep(3600)  # the long poll, held open
        elif method == "sendMessage":
            self._message_id += 1
            result = {"message_id": self._message_id, "chat": {"id": params["chat_id"]}, "text": params["text"]}
        return httpx.Response(200, json={"ok": True, "result": result})

    def sent(self, chat_id: int = CHAT) -> list[dict]:
        return [p for m, p in self.requests if m == "sendMessage" and p["chat_id"] == chat_id]

    def answers(self) -> list[str]:
        return [p["text"] for m, p in self.requests if m == "answerCallbackQuery"]


class TelegramServer(Server):
    def __init__(self, name: str, clock: Clock | None = None):
        self.db, self.clock, self.sender = _TMP / f"telegram-{name}.db", clock or Clock(), FakeSender()
        self.fake = FakeTelegram()
        self.api = TelegramApi(TOKEN, http=httpx.AsyncClient(transport=httpx.MockTransport(self.fake.handler)))
        self.mailbox = Mailbox(self.db, sender=self.sender, telegram=self.api, public_url=PUBLIC_URL,
                               clock=self.clock)
        self.mailbox.set_user(USER, "correct horse battery")
        self.app = create_app(self.mailbox, vapid_public_key="test-key", secure_cookies=False, run_engines=False)
        self.bot = TelegramBot(self.mailbox, self.api)
        self._update = 0

    def update(self, **body) -> dict:
        self._update += 1
        return {"update_id": self._update, **body}

    async def say(self, text: str, chat_id: int = CHAT, chat_type: str = "private") -> None:
        self.last = self.update(message={
            "message_id": self._update + 1, "chat": {"id": chat_id, "type": chat_type}, "from": {"id": chat_id},
            "text": text})
        await self.bot.handle(self.last)

    async def tap(self, data: str, message: dict, chat_id: int = CHAT, query_id: str | None = None) -> None:
        await self.bot.handle(self.update(callback_query={
            "id": query_id or f"cb{self._update}", "from": {"id": chat_id}, "data": data,
            "message": {**message, "chat": {"id": chat_id, "type": "private"}}}))

    async def link(self, browser: httpx.AsyncClient, chat_id: int = CHAT) -> None:
        code = (await browser.post("/api/pairing-codes", json={})).json()["code"]
        await self.say(f"/link {code}", chat_id)
        assert self.mailbox.telegram_user(chat_id) == USER


async def _proposal(target: str, batch: str | None = None) -> str:
    payload = {"diff": DIFF.replace("Run tests", "DIFF_MARKER run tests"), "rationale": RATIONALE,
               "evidence": [EVIDENCE], **({"batch": batch} if batch else {})}
    p = await Proposals(get_store()).create(target, payload, request_id=f"gardener:telegram:{uuid.uuid4().hex}",
                                            actor="gardener", expires_at=datetime.now(UTC) + timedelta(days=7))
    return p.id


# ── linking and unlinked chats ────────────────────────────────────────────────

async def check_linking():
    server = TelegramServer("linking")
    async with server.browser() as browser:
        code = (await browser.post("/api/pairing-codes", json={})).json()["code"]
        await server.say(f"/link {code}", chat_type="group")  # a group: everyone in it could tap
        assert server.mailbox.telegram_user(CHAT) is None and server.fake.requests == []
        await server.say(f"/link {code.lower()}")
        assert server.mailbox.telegram_user(CHAT) == USER
        assert server.fake.sent()[-1]["text"].startswith(f"Linked to {USER}.")
        # The code is spent: it neither links another chat nor pairs a device.
        await server.say(f"/link {code}", STRANGER)
        assert server.mailbox.telegram_user(STRANGER) is None
        assert "unknown, used or expired" in server.fake.sent(STRANGER)[-1]["text"]
        async with server.http() as http:
            r = await http.post("/api/device/pair", json={"code": code, "name": "laptop"})
            assert r.status_code == 403, r.text
        await server.say("/unlink")
        assert server.mailbox.telegram_user(CHAT) is None
        await server.link(browser)
    print("✓ telegram: /link spends the dashboard's one-time code in a private chat; /unlink undoes it")


async def check_unlinked_chat_ignored():
    server = TelegramServer("unlinked")
    async with server.browser() as browser:
        await server.link(browser)
        pid = await _proposal("wisen:CLAUDE.md")
        creds = await server.device(browser)
        async with server.http() as http:
            await worker.run(creds, get_store(), http=http, once=True)
        before = len(server.fake.requests)
        jobs = len(server.mailbox.device_jobs(USER, ("queued", "leased")))
        await server.say("what did I do yesterday?", STRANGER)
        await server.say("/search pagination", STRANGER)
        await server.tap(f"approve:{pid}", {"message_id": 1, "text": "Proposal"}, STRANGER)
        assert len(server.fake.requests) == before, server.fake.requests[before:]
        assert len(server.mailbox.device_jobs(USER, ("queued", "leased"))) == jobs
        assert (await Proposals(server.mailbox.store(USER)).get(pid)).state == "proposed"
        # A tap on the linked chat's message, sent by someone else.
        await server.bot.handle(server.update(callback_query={
            "id": "forged", "from": {"id": STRANGER}, "data": f"approve:{pid}",
            "message": {"message_id": 1, "text": "Proposal", "chat": {"id": CHAT, "type": "private"}}}))
        assert (await Proposals(server.mailbox.store(USER)).get(pid)).state == "proposed"
        assert len(server.fake.requests) == before
    print("✓ telegram: an unlinked chat is ignored: no reply, no question, no decision")


# ── notifications and decisions ───────────────────────────────────────────────

async def check_notifications_carry_no_diff():
    server = TelegramServer("notify")
    store = get_store()
    async with server.browser() as browser:
        await server.link(browser)
        creds = await server.device(browser)
        # A day's batch, uploaded before its reflection, then announced with it.
        made = [await _proposal(t, "2026-10-21") for t in ("wisen:CLAUDE.md", "wisen:AGENTS.md")]
        await _proposal("wisen:.claude/skills/run/SKILL.md")  # not in a batch: sent on its own
        async with server.http() as http:
            await worker.run(creds, store, http=http, once=True)
            await server.run_jobs()
            assert [m["text"].split("\n")[0] for m in server.fake.sent()[1:]] == [
                "Proposal: wisen:.claude/skills/run/SKILL.md"], server.fake.sent()
            await _upsert(_reflection("2026-10-21"))
            await worker.run(creds, store, http=http, once=True)
            await server.run_jobs()
        messages = server.fake.sent()[2:]
        assert messages[0]["text"].startswith("Reflection ready\n2026-10-21 · mild-friction · 2 proposals waiting")
        assert messages[0]["text"].endswith(f"{PUBLIC_URL}/#day-2026-10-21") and "reply_markup" not in messages[0]
        by_target = {m["text"].split("\n")[0]: m for m in messages[1:]}
        assert set(by_target) == {"Proposal: wisen:CLAUDE.md", "Proposal: wisen:AGENTS.md"}, by_target
        for pid, target in zip(made, ("wisen:CLAUDE.md", "wisen:AGENTS.md"), strict=True):
            message = by_target[f"Proposal: {target}"]
            _, description, link = message["text"].split("\n\n")
            assert len(description) == DESCRIPTION_CAP and description.endswith("…")
            assert RATIONALE.startswith(description[:-1]) and link == f"{PUBLIC_URL}/#proposal-{pid}"
            assert message["reply_markup"]["inline_keyboard"] == [[
                {"text": "Approve", "callback_data": f"approve:{pid}"},
                {"text": "Reject", "callback_data": f"reject:{pid}"}]]
        # Nothing of the diff or the evidence ever reached Telegram.
        wire = json.dumps(server.fake.requests)
        for marker in ("DIFF_MARKER", "EVIDENCE_MARKER", "+++ b/CLAUDE.md", "@@ -1 +1,2 @@"):
            assert marker not in wire, marker
    print("✓ telegram: a proposal message is target + capped rationale + link + buttons, never diff or evidence")


async def check_one_decision_across_channels():
    server = TelegramServer("decide")
    store = get_store()
    async with server.browser() as browser:
        await server.link(browser)
        creds = await server.device(browser)
        first, second = await _proposal("wisen:CLAUDE.md"), await _proposal("wisen:AGENTS.md")
        async with server.http() as http:
            await worker.run(creds, store, http=http, once=True)
            await server.run_jobs()
            messages = {m["text"].split("\n")[0]: m for m in server.fake.sent()}
            card = {"message_id": 7, "text": messages["Proposal: wisen:CLAUDE.md"]["text"]}

            # Telegram first, then the web app: one decision, the web app hears it was decided.
            await server.tap(f"approve:{first}", card, query_id="tap-1")
            assert server.fake.answers()[-1] == "✓ Approved"
            edit = [p for m, p in server.fake.requests if m == "editMessageText"][-1]
            assert edit["message_id"] == 7 and edit["text"].endswith("\n\n✓ Approved") and "reply_markup" not in edit
            r = await browser.post(f"/api/proposals/{first}/decision",
                                   json={"decision": "rejected", "request_id": "web-after-telegram"})
            web = r.json()
            assert web["decision"] == "approved" and web["already_decided"] and web["actor"] == f"telegram:{USER}", web
            # A retried tap (Telegram redelivers the update) returns the first result.
            await server.tap(f"approve:{first}", card, query_id="tap-1")
            assert server.fake.answers()[-1] == "✓ Approved"
            history = await Proposals(server.mailbox.store(USER)).history(first)
            assert [e.to_state for e in history] == ["proposed", "approved"], history
            with server.mailbox.store(USER).transaction() as conn:
                assert conn.execute("SELECT COUNT(*) FROM proposal_decisions WHERE proposal_id = ?",
                                    (first,)).fetchone()[0] == 1

            # The web app first, then a tap: Telegram says it was already decided.
            await browser.post(f"/api/proposals/{second}/decision",
                               json={"decision": "rejected", "request_id": "web-first", "note": "not now"})
            await server.tap(f"approve:{second}", {"message_id": 8, "text": "Proposal: wisen:AGENTS.md"})
            assert server.fake.answers()[-1] == "Already rejected in the web app"
            assert (await Proposals(server.mailbox.store(USER)).get(second)).state == "rejected"

            # The laptop hears each decision once, whichever channel made it.
            await worker.run(creds, store, http=http, once=True)
            local = Proposals(store)
            assert [e.request_id for e in await local.history(first)][1] == "telegram:tap-1"
            assert (await local.get(second)).state == "rejected"
    print("✓ telegram: approving in Telegram and then in the web app records one decision")


# ── history chat ──────────────────────────────────────────────────────────────

def _fake_chat_model(tools_seen: list[list[str]]):
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    def reply(messages, info):
        tools_seen.append(sorted(t.name for t in info.function_tools))
        question = messages[-1].parts[-1].content.rsplit("\n", 1)[-1]
        return ModelResponse(parts=[TextPart(f"On 2026-10-21 you said \"keep it short\". (re: {question})")])

    return FunctionModel(reply)


async def check_history_chat():
    from wisen.chat import agent as chat

    clock = Clock()
    server = TelegramServer("history", clock)
    store = get_store()
    os.environ["WISEN_MODEL"] = "test"  # the agents are built on it; each run below overrides the model
    tools_seen: list[list[str]] = []
    model = _fake_chat_model(tools_seen)
    async with server.browser() as browser:
        await server.link(browser)
        creds = await server.device(browser)
        await _upsert(_reflection("2026-10-21"))
        # The laptop went to sleep two hours ago.
        clock.now += timedelta(hours=2)
        await server.say("/search pagination cursor")
        assert server.fake.sent()[-1]["text"] == "Queued: your laptop is offline. I'll answer when it wakes."
        (job,) = server.mailbox.device_jobs(USER, ("queued",))
        assert job.kind == "search" and job.args == {"question": "pagination cursor"}, job
        # A redelivered update asks once.
        await server.bot.handle(server.last)
        assert len(server.mailbox.device_jobs(USER, ("queued",))) == 1
        # It wakes: the worker answers on the laptop and the answer reaches the chat.
        with chat.answer_agent(True).override(model=model), chat.answer_agent(False).override(model=model):
            async with server.http() as http:
                await worker.run(creds, store, http=http, once=True)
                await server.run_jobs()
                assert server.fake.sent()[-1]["text"] == (
                    "On 2026-10-21 you said \"keep it short\". (re: pagination cursor)"), server.fake.sent()[-1]
                assert server.mailbox.device_jobs(USER, ("succeeded",))[0].id == job.id
                # A plain question is answered from reflections only.
                await server.say("how was my week?")
                assert server.fake.sent()[-1]["text"] == "Asking your laptop…"
                await worker.run(creds, store, http=http, once=True)
                await server.run_jobs()
                assert server.fake.sent()[-1]["text"].endswith("(re: how was my week?)")
                # A re-run job's second answer is accepted; the first one stays the only message.
                messages = len(server.fake.sent())
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url=BASE) as raw:
                    from wisen.cloud.client import CloudClient, Refused
                    from wisen.packets import answer_packet
                    async with CloudClient(creds, raw) as client:
                        stored = await client.send_packet(answer_packet(job.id, "a second answer", user=USER,
                                                                        device_id=creds.device_id))
                        assert stored["stored"] is False, stored
                        await server.run_jobs()
                        assert len(server.fake.sent()) == messages, server.fake.sent()[messages:]
                        try:
                            await client.send_packet(answer_packet(uuid.uuid4().hex, "an answer", user=USER,
                                                                   device_id=creds.device_id))
                            raise AssertionError("an answer to no question must be refused")
                        except Refused as e:
                            assert "409" in str(e)
    assert tools_seen == [
        ["get_reflection", "list_reflections", "search_agent_configs", "search_chat_history"],
        ["get_reflection", "list_reflections"],
    ], tools_seen
    print("✓ telegram: /search while the laptop sleeps is answered after it wakes; questions use reflections only")


# ── the polling loop and the token ────────────────────────────────────────────

async def check_poll_loop_and_token():
    server = TelegramServer("loop")
    logs = io.StringIO()
    handler = logging.StreamHandler(logs)
    httpx_log = logging.getLogger("httpx")
    httpx_log.addHandler(handler)
    httpx_log.setLevel(logging.DEBUG)
    try:
        async with server.browser() as browser:
            code = (await browser.post("/api/pairing-codes", json={})).json()["code"]
        server.fake.updates = [
            server.update(message={"message_id": 1, "chat": {"id": CHAT, "type": "private"}, "text": f"/link {code}"}),
            # A malformed update that raises: it is skipped and the loop goes on.
            server.update(callback_query={"from": {"id": CHAT}, "data": "approve:x",
                                          "message": {"chat": {"id": CHAT, "type": "private"}}}),
            server.update(message={"message_id": 2, "chat": {"id": CHAT, "type": "private"}, "text": "/help"}),
        ]
        task = asyncio.create_task(server.bot.run_forever())
        await asyncio.wait_for(server.fake.polled.wait(), 5)
        task.cancel()
        polls = [p for m, p in server.fake.requests if m == "getUpdates"]
        assert [p["offset"] for p in polls] == [None, 4], polls  # each update confirmed by the next call
        assert server.mailbox.telegram_user(CHAT) == USER
        assert [m["text"].split("\n")[0] for m in server.fake.sent()] == [f"Linked to {USER}.", HELP.split("\n")[0]]
        # A failed send: the error and the stored job name the method, never the token.
        server.fake.fail.add("sendMessage")
        try:
            await server.api.send(CHAT, "hi")
            raise AssertionError("a failed call must raise")
        except TelegramError as e:
            assert str(e) == "sendMessage: 400 Bad Request: nope", e
        await _upsert(_reflection("2026-10-22"))
        async with server.browser() as browser:
            creds = await server.device(browser)
            async with server.http() as http:
                await worker.run(creds, get_store(), http=http, once=True)
        await server.run_jobs()
    finally:
        httpx_log.removeHandler(handler)
        httpx_log.setLevel(logging.NOTSET)
    with server.mailbox.store(USER).transaction() as conn:
        errors = [r["error"] for r in conn.execute("SELECT error FROM jobs WHERE kind = 'telegram'")]
    assert errors and all(e and "Bad Request" in e for e in errors), errors
    assert "HTTP Request: POST" in logs.getvalue() and TOKEN not in logs.getvalue(), logs.getvalue()
    assert TOKEN.encode() not in _db_bytes(server.db)
    print("✓ telegram: the poll loop confirms each update; the token is in no log, error or database row")


async def main():
    await check_linking()
    await check_unlinked_chat_ignored()
    await check_notifications_carry_no_diff()
    await check_one_decision_across_channels()
    await check_history_chat()
    await check_poll_loop_and_token()
    print("\nall telegram tests passed")


if __name__ == "__main__":
    asyncio.run(main())
