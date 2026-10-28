"""
`wisen cloud serve`: the server's one process. Behind a reverse proxy (Caddy)
that terminates HTTPS; binds 127.0.0.1 and runs one uvicorn worker, because
this process owns the SQLite file and the timers.

Browser (session cookie, after /login):
  GET  /                           dashboard + approval inbox (installable web app)
  GET  /api/logs                   reflections (result packets), newest first
  GET  /api/usage                  latest usage packet
  GET  /api/queue                  dates waiting for a device to synthesize
  POST /api/trigger                {"date"}: ask a device to synthesize that day
  GET  /api/proposals              the approval inbox and change log (each proposal with
                                   its decision, latest outcome and publish job's state)
  GET  /api/timeline               daily verification metrics + merged Gardener changes
  POST /api/proposals/{id}/decision  {"decision", "note", "request_id"}
  POST /api/proposals/{id}/publish   no body; 202 {"status": "publishing"}: the laptop
                                   pushes the applied branch and opens its pull request
  GET  /api/devices                paired devices
  POST /api/devices/{id}/revoke
  POST /api/pairing-codes          a one-time code for `wisen cloud pair`
  GET  /api/push                   the VAPID public key (null: push is off)
  POST /api/push/subscriptions     {"subscription"} from PushManager.subscribe()
  POST /api/push/unsubscribe       {"endpoint"}

Device (Authorization: Bearer <device token>; the laptop only connects out):
  POST /api/device/pair            {"code", "name"} -> a device token (no auth)
  POST /api/device/packets         one packet (result, usage, proposal, proposal_update, metrics, answer)
  POST /api/device/poll            long poll: a device job to lease, and decisions
  POST /api/device/jobs/{id}/heartbeat
  POST /api/device/jobs/{id}/finish  a job_status packet

Environment (no model key: stage A makes no model calls):
  WISEN_CLOUD_DB            SQLite file (default ~/.wisen/cloud/wisen.db)
  WISEN_CLOUD_PUBLIC_URL    e.g. https://wisen.example.org; https makes the
                            session cookie Secure, and it is the VAPID contact
  WISEN_VAPID_PRIVATE_KEY   from `wisen cloud vapid-keys`; both unset = no push
  WISEN_VAPID_PUBLIC_KEY
  WISEN_VAPID_SUBJECT       optional VAPID contact (mailto: or https URL)
  WISEN_TELEGRAM_BOT_TOKEN  from @BotFather; unset = no Telegram bot. The bot
                            long-polls Telegram, so it adds no endpoint here.
"""
import asyncio
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from wisen.cloud.backup import serving
from wisen.cloud.mailbox import ACCEPTED, Conflict, Mailbox, NotPublishable
from wisen.cloud.push import WebPushSender
from wisen.cloud.telegram import TelegramApi, TelegramBot
from wisen.packets import PacketRejected, receive
from wisen.proposals import IllegalTransition

STATIC = Path(__file__).resolve().parent.parent / "dashboard"
COOKIE = "wisen_session"
MAX_BODY = 1_000_000  # bytes; a packet is far smaller
MAX_WAIT = 30.0  # seconds a device poll may hang; under the proxy's timeouts
LOGIN_WINDOW, LOGIN_TRIES = 15 * 60, 5  # per user id, per process
# Provider keys the laptop uses. The stage-A server needs none; /health
# reports whether one is set anyway, so a deploy can prove it has none.
MODEL_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "DEEPSEEK_API_KEY")

Short = Annotated[str, StringConstraints(max_length=200)]
Id = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(_Body):
    user: Short
    password: Annotated[str, StringConstraints(max_length=1000)]


class Trigger(_Body):
    date: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]


class DecisionIn(_Body):
    decision: Literal["approved", "rejected"]
    note: Annotated[str, StringConstraints(max_length=2000)] | None = None
    request_id: Annotated[str, StringConstraints(min_length=8, max_length=100)]


class PushKeys(BaseModel):
    p256dh: Annotated[str, StringConstraints(max_length=200)]
    auth: Annotated[str, StringConstraints(max_length=100)]


class PushSubscription(BaseModel):
    endpoint: Annotated[str, StringConstraints(pattern=r"^https://", max_length=1000)]
    keys: PushKeys


class Subscribe(_Body):
    subscription: PushSubscription


class Unsubscribe(_Body):
    endpoint: Annotated[str, StringConstraints(max_length=1000)]


class Pair(_Body):
    code: Annotated[str, StringConstraints(max_length=40)]
    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]


class Poll(_Body):
    cursor: int = Field(0, ge=0)
    wait: float = Field(25.0, ge=0, le=MAX_WAIT)


def create_app(
    mailbox: Mailbox,
    *,
    vapid_public_key: str | None = None,
    secure_cookies: bool = True,
    run_engines: bool = True,
) -> FastAPI:
    """The app around one mailbox. `run_engines=False` leaves the job engines
    and the schedule to the caller (tests drive them step by step)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with serving(mailbox.path):
            tasks = [asyncio.create_task(mailbox.run_forever())] if run_engines else []
            if run_engines and mailbox.telegram:
                tasks.append(asyncio.create_task(TelegramBot(mailbox, mailbox.telegram).run_forever()))
            yield
            for task in tasks:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="wisen cloud", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    failures: dict[str, deque] = defaultdict(deque)

    @app.middleware("http")
    async def limits(request: Request, call_next):
        if request.method == "POST" and not request.headers.get("content-type", "").startswith("application/json"):
            # JSON only: a cross-site form post cannot send it without a
            # CORS preflight, which this app never grants.
            return JSONResponse({"detail": "send application/json"}, status_code=415)
        return await call_next(request)

    app.add_middleware(_BodyLimit)

    async def web_user(request: Request) -> str:
        token = request.cookies.get(COOKIE)
        user_id = await asyncio.to_thread(mailbox.session_user, token) if token else None
        if user_id is None:
            raise HTTPException(401, "log in first")
        return user_id

    async def device(request: Request) -> tuple[str, str]:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        found = await asyncio.to_thread(mailbox.device_for_token, token) if scheme.lower() == "bearer" else None
        if found is None:
            raise HTTPException(401, "unknown or revoked device token", headers={"WWW-Authenticate": "Bearer"})
        return found

    User = Annotated[str, Depends(web_user)]
    Device = Annotated[tuple[str, str], Depends(device)]

    # ── pages ─────────────────────────────────────────────────────────────────

    @app.get("/")
    async def index(request: Request):
        token = request.cookies.get(COOKIE)
        if not token or not await asyncio.to_thread(mailbox.session_user, token):
            return RedirectResponse("/login", status_code=303)
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/login")
    async def login_page():
        return FileResponse(STATIC / "login.html")

    @app.get("/manifest.webmanifest")
    async def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js")
    async def service_worker():
        return FileResponse(STATIC / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    for name in ("icon.svg", "icon-192.png", "icon-512.png"):
        app.add_api_route(f"/{name}", lambda name=name: FileResponse(
            STATIC / name, headers={"Cache-Control": "max-age=86400"}), methods=["GET"])

    @app.get("/health")
    async def health():
        return {"ok": True, "push": vapid_public_key is not None, "telegram": mailbox.telegram is not None,
                "model_key": any(os.environ.get(k) for k in MODEL_KEYS)}

    # ── browser session ───────────────────────────────────────────────────────

    @app.post("/login")
    async def login(body: Login, response: Response):
        tries, now = failures[body.user], time.monotonic()
        while tries and tries[0] < now - LOGIN_WINDOW:
            tries.popleft()
        if len(tries) >= LOGIN_TRIES:
            raise HTTPException(429, "too many failed logins; try again later")
        if not await asyncio.to_thread(mailbox.check_login, body.user, body.password):
            if len(failures) > 10_000:  # bound the memory a stream of made-up names can take
                failures.clear()
            tries.append(now)
            raise HTTPException(401, "wrong user or password")
        tries.clear()
        token = await asyncio.to_thread(mailbox.new_session, body.user)
        response.set_cookie(COOKIE, token, max_age=30 * 24 * 3600, httponly=True, secure=secure_cookies,
                            samesite="lax")
        return {"ok": True}

    @app.post("/logout")
    async def logout(request: Request, response: Response):
        if token := request.cookies.get(COOKIE):
            await asyncio.to_thread(mailbox.end_session, token)
        response.delete_cookie(COOKIE)
        return {"ok": True}

    # ── dashboard and approval inbox ──────────────────────────────────────────

    @app.get("/api/logs")
    async def logs(user: User):
        return await asyncio.to_thread(mailbox.packets, user, "result")

    @app.get("/api/usage")
    async def usage(user: User):
        latest = await asyncio.to_thread(mailbox.packets, user, "usage", 1)
        return latest[0] if latest else {}

    @app.get("/api/queue")
    async def queue(user: User):
        jobs = await asyncio.to_thread(mailbox.device_jobs, user, ("queued", "leased"))
        return sorted({j.args["date"] for j in jobs if j.kind == "synthesize"})

    @app.post("/api/trigger")
    async def trigger(body: Trigger, user: User):
        await mailbox.enqueue_device_job(user, "synthesize", {"date": body.date, "force": True},
                                         request_id=f"trigger-synthesis:{body.date}", rerun=True)
        return {"queued": body.date}

    @app.get("/api/proposals")
    async def proposals(user: User):
        publishing = await asyncio.to_thread(mailbox.publish_jobs, user)
        details = await asyncio.to_thread(mailbox.inbox_details, user)
        return [{**asdict(p), "publish": publishing.get(p.id), **details.get(p.id, {"decision": None, "outcome": None})}
                for p in await mailbox.inbox(user)]

    @app.get("/api/timeline")
    async def timeline(user: User):
        return await asyncio.to_thread(mailbox.timeline, user)

    @app.post("/api/proposals/{proposal_id}/decision")
    async def decide(proposal_id: Id, body: DecisionIn, user: User):
        try:
            decision = await mailbox.decide(user, proposal_id, body.decision, request_id=body.request_id,
                                            actor=f"web:{user}", note=body.note)
        except IllegalTransition as e:
            raise HTTPException(409, str(e)) from None
        except ValueError as e:  # request id reused for a different command
            raise HTTPException(409, str(e)) from None
        return {**asdict(decision), "already_decided": decision.request_id != body.request_id}

    @app.post("/api/proposals/{proposal_id}/publish", status_code=202)
    async def publish(proposal_id: Id, user: User):
        try:
            await mailbox.publish(user, proposal_id)
        except LookupError as e:
            raise HTTPException(404, str(e)) from None
        except NotPublishable as e:
            raise HTTPException(409, str(e)) from None
        return {"status": "publishing"}

    @app.get("/api/devices")
    async def devices(user: User):
        return await asyncio.to_thread(mailbox.devices, user)

    @app.post("/api/devices/{device_id}/revoke")
    async def revoke(device_id: Id, user: User):
        if not await asyncio.to_thread(mailbox.revoke_device, user, device_id):
            raise HTTPException(404, "no such active device")
        return {"revoked": device_id}

    @app.post("/api/pairing-codes")
    async def pairing_code(user: User):
        code, expires_at = await asyncio.to_thread(mailbox.new_pairing_code, user)
        return {"code": code, "expires_at": expires_at}

    @app.get("/api/push")
    async def push_key(user: User):
        return {"public_key": vapid_public_key}

    @app.post("/api/push/subscriptions")
    async def subscribe(body: Subscribe, user: User):
        await asyncio.to_thread(mailbox.add_push_subscription, user, body.subscription.model_dump())
        return {"ok": True}

    @app.post("/api/push/unsubscribe")
    async def unsubscribe(body: Unsubscribe, user: User):
        await asyncio.to_thread(mailbox.remove_push_subscription, user, body.endpoint)
        return {"ok": True}

    # ── device API ────────────────────────────────────────────────────────────

    @app.post("/api/device/pair")
    async def pair(body: Pair):
        paired = await asyncio.to_thread(mailbox.pair, body.code, body.name)
        if paired is None:
            raise HTTPException(403, "pairing code unknown, used or expired")
        return paired

    @app.post("/api/device/packets")
    async def packets(request: Request, dev: Device):
        user_id, device_id = dev
        packet = _receive(await _json(request), ACCEPTED)
        try:
            stored = await asyncio.to_thread(mailbox.store_packet, user_id, device_id, packet)
        except Conflict as e:
            raise HTTPException(409, str(e)) from None
        if stored and packet.packet_type in ("result", "proposal", "proposal_update", "answer"):
            mailbox.engine(user_id).wake()
        return {"stored": stored, "packet_id": packet.packet_id}

    @app.post("/api/device/poll")
    async def poll(body: Poll, dev: Device):
        user_id, device_id = dev
        job, events = await mailbox.poll(user_id, device_id, body.cursor, body.wait)
        return {
            "job": {"id": job.id, "kind": job.kind, "args": job.args, "attempts": job.attempts} if job else None,
            "decisions": [{"event_id": n, "proposal_id": e.proposal_id, "state": e.to_state, "note": e.note,
                           "request_id": e.request_id, "actor": e.actor, "at": e.at} for n, e in events],
            "cursor": events[-1][0] if events else body.cursor,
        }

    @app.post("/api/device/jobs/{job_id}/heartbeat")
    async def heartbeat(job_id: Id, dev: Device):
        if not await mailbox.heartbeat(*dev, job_id):
            raise HTTPException(409, "this device no longer holds the job")
        return {"ok": True}

    @app.post("/api/device/jobs/{job_id}/finish")
    async def finish(job_id: Id, request: Request, dev: Device):
        status = _receive(await _json(request), ("job_status",))
        try:
            done = await mailbox.finish(*dev, job_id, status)
        except Conflict as e:
            raise HTTPException(409, str(e)) from None
        if not done:
            raise HTTPException(409, "this device no longer holds the job")
        return {"ok": True}

    return app


class _BodyLimit:
    """MAX_BODY for every request: refused up front by Content-Length, or
    counted as a chunked body streams in, before any route parses it."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        too_large = JSONResponse({"detail": "request too large"}, status_code=413)
        if int(dict(scope["headers"]).get(b"content-length") or 0) > MAX_BODY:
            return await too_large(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return
            body += message.get("body", b"")
            if len(body) > MAX_BODY:
                return await too_large(scope, receive, send)
            if not message.get("more_body"):
                break
        replayed = False

        async def replay():
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)


async def _json(request: Request) -> object:
    try:
        return await request.json()
    except ValueError:
        raise HTTPException(400, "body is not JSON") from None


def _receive(data: object, accepted) -> object:
    try:
        return receive(data, accepted)
    except PacketRejected as e:
        raise HTTPException(422, str(e)) from None


def from_env(db: Path | None = None, *, run_engines: bool = True) -> FastAPI:
    """The app as `wisen cloud serve` builds it, configured by environment."""
    public_url = os.environ.get("WISEN_CLOUD_PUBLIC_URL", "").rstrip("/")
    private, public = os.environ.get("WISEN_VAPID_PRIVATE_KEY"), os.environ.get("WISEN_VAPID_PUBLIC_KEY")
    subject = os.environ.get("WISEN_VAPID_SUBJECT") or public_url
    sender = None
    if private and public:
        if not subject:
            raise SystemExit("Web Push needs a VAPID contact: set WISEN_VAPID_SUBJECT or WISEN_CLOUD_PUBLIC_URL")
        sender = WebPushSender(private, subject)
    else:
        print("wisen cloud: no VAPID keys (WISEN_VAPID_PRIVATE_KEY/PUBLIC_KEY); Web Push is off")
    if any(os.environ.get(k) for k in MODEL_KEYS):
        print(f"wisen cloud: warning: a model API key is set ({', '.join(k for k in MODEL_KEYS if os.environ.get(k))});"
              " the stage-A server makes no model calls and should not hold one")
    token = os.environ.get("WISEN_TELEGRAM_BOT_TOKEN")
    if not token:
        print("wisen cloud: no WISEN_TELEGRAM_BOT_TOKEN; the Telegram bot is off")
    elif not public_url:
        print("wisen cloud: warning: no WISEN_CLOUD_PUBLIC_URL; Telegram messages will carry no dashboard link")
    db = db or Path(os.environ.get("WISEN_CLOUD_DB", "~/.wisen/cloud/wisen.db")).expanduser()
    mailbox = Mailbox(db, sender=sender, telegram=TelegramApi(token) if token else None, public_url=public_url)
    return create_app(mailbox, vapid_public_key=public if sender else None,
                      secure_cookies=public_url.startswith("https://"), run_engines=run_engines)
