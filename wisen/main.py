import asyncio
import json
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from wisen.chat.agent import stream_chat
from wisen.jobs import Job, get_engine
from wisen.store import get_store
from wisen.synthesis.job import enqueue_day
from wisen.synthesis.schema import Reflection


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The job worker runs inside the API process; jobs live in SQLite, so a
    # restart picks up where the last process stopped.
    worker = asyncio.create_task(get_engine().run_forever())
    yield
    worker.cancel()
    with suppress(asyncio.CancelledError):
        await worker


app = FastAPI(title="Wisen API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class SynthesizeRequest(BaseModel):
    date: str | None = None  # YYYY-MM-DD, defaults to today
    request_id: str | None = None  # resend with the same id to get the same job back; without one, the day re-synthesizes


class JobStatus(BaseModel):
    job_id: str
    status: str        # running | done | error
    date: str | None = None
    error: str | None = None


_STATUS = {"succeeded": "done", "failed": "error"}  # queued, leased, waiting -> running


def _job_status(job: Job) -> JobStatus:
    return JobStatus(
        job_id=job.id,
        status=_STATUS.get(job.state, "running"),
        date=job.args.get("date"),
        error=job.error if job.state == "failed" else None,
    )


@app.post("/synthesize", response_model=JobStatus)
async def trigger_synthesis(req: SynthesizeRequest):
    date = req.date or datetime.now(UTC).date().isoformat()
    try:
        if req.request_id:
            job = await get_engine().enqueue("synthesize", {"date": date}, request_id=req.request_id)
        else:
            job = await enqueue_day(date)
    except ValueError as e:  # request id reused for a different date
        raise HTTPException(status_code=409, detail=str(e))
    return _job_status(job)


@app.get("/synthesize/{job_id}", response_model=JobStatus)
async def get_synthesis_status(job_id: str):
    job = await get_engine().get(job_id)
    if not job or job.kind != "synthesize":
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_status(job)


@app.get("/reflections", response_model=list[dict])
async def list_reflections():
    fields = ("date", "alignment_score", "alignment_label", "synthesized_at")
    docs = await get_store().list_reflections(limit=30)
    return [{k: d[k] for k in fields if k in d} for d in docs]


@app.get("/reflections/{date}", response_model=Reflection)
async def get_reflection(date: str):
    doc = await get_store().get_reflection(date)
    if not doc:
        raise HTTPException(status_code=404, detail=f"No reflection for {date}")
    doc.pop("synthesized_at", None)
    return Reflection(**doc)


class ChatMessage(BaseModel):
    role: str   # "user" | "assistant"
    content: str

class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    context_date: str | None = None
    session_id: str | None = None


@app.post("/chat")
async def chat(req: ChatRequest):
    if not req.messages or req.messages[-1].role != "user":
        raise HTTPException(status_code=400, detail="Last message must be role=user")
    messages = [{"role": m.role, "content": m.content} for m in req.messages]
    store = get_store()
    now = datetime.now(UTC)

    # Resolve or create session
    session_id = req.session_id
    if session_id and not await store.chat_session_exists(session_id):
        session_id = None
    if not session_id:
        session_id = str(uuid.uuid4())
        await store.create_chat_session(session_id, "text", now)

    # Save user message immediately
    user_msg = messages[-1]
    await store.append_chat_message(session_id, user_msg["role"], user_msg["content"], now)

    async def event_stream():
        yield f"data: {json.dumps({'type': 'session_id', 'session_id': session_id})}\n\n"
        assistant_tokens: list[str] = []
        try:
            async for event in stream_chat(messages, req.context_date):
                if event.get("type") == "error":
                    yield f"data: {json.dumps({'error': event.get('message', 'Unknown error')})}\n\n"
                else:
                    if event.get("type") == "token":
                        assistant_tokens.append(event["data"])
                    yield f"data: {json.dumps(event)}\n\n"
        except (ValueError, RuntimeError) as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            if assistant_tokens:
                full = "".join(assistant_tokens)
                await store.append_chat_message(session_id, "assistant", full, datetime.now(UTC))
            yield f"data: {json.dumps({'done': True})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/chat/sessions")
async def list_chat_sessions(limit: int = 20):
    docs = await get_store().list_chat_sessions(limit)
    for d in docs:
        # Surface first user message as preview
        first = d.pop("first_message")
        d["preview"] = first["content"][:80] if first and first["role"] == "user" else ""
    return docs


@app.get("/chat/sessions/{session_id}")
async def get_chat_session(session_id: str):
    doc = await get_store().get_chat_session(session_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Session not found")
    return doc


@app.post("/voice/token")
async def voice_token():
    """Return a LiveKit JWT for the renderer to join the voice room."""
    try:
        from wisen.voice.token import LIVEKIT_URL, create_participant_token
    except ImportError:
        raise HTTPException(status_code=501, detail="voice extra not installed: pip install 'wisen[voice]'")
    token, room_name = create_participant_token()
    return {"token": token, "url": LIVEKIT_URL, "room": room_name}


_MODEL_KEY_ENV = {
    "google": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


@app.get("/health")
async def health():
    import asyncio
    import os
    import sqlite3

    from wisen.llm import get_model

    status = {"storage": False}
    try:
        status["storage"] = await asyncio.wait_for(get_store().ping(), timeout=2.0)
    except (TimeoutError, OSError, sqlite3.Error):
        pass  # store unreachable, status already False
    model = get_model()
    provider = model.split(":", 1)[0]
    status["model"] = model
    status["llm_key"] = bool(os.getenv(_MODEL_KEY_ENV.get(provider, ""), ""))
    return status
