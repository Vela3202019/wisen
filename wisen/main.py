import json
import uuid
from datetime import UTC, datetime

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from wisen.chat.agent import stream_chat
from wisen.ingestion.pipeline import ingest_all
from wisen.store import get_store
from wisen.synthesis.agent import synthesize
from wisen.synthesis.schema import Reflection

app = FastAPI(title="Wisen API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory job tracker
_jobs: dict[str, dict] = {}


class SynthesizeRequest(BaseModel):
    date: str | None = None  # YYYY-MM-DD, defaults to today


class JobStatus(BaseModel):
    job_id: str
    status: str        # running | done | error
    date: str | None = None
    error: str | None = None


async def _run_synthesis(job_id: str, date: str):
    try:
        result = await ingest_all(date)
        reflection = await synthesize(result, date)
        _jobs[job_id] = {"status": "done", "date": date, "reflection": reflection.model_dump()}
    except (ValueError, RuntimeError) as e:
        _jobs[job_id] = {"status": "error", "date": date, "error": str(e)}


@app.post("/synthesize", response_model=JobStatus)
async def trigger_synthesis(req: SynthesizeRequest, background_tasks: BackgroundTasks):
    date = req.date or datetime.now(UTC).date().isoformat()
    job_id = f"{date}-{datetime.now(UTC).strftime('%H%M%S')}"
    _jobs[job_id] = {"status": "running", "date": date}
    background_tasks.add_task(_run_synthesis, job_id, date)
    return JobStatus(job_id=job_id, status="running", date=date)


@app.get("/synthesize/{job_id}", response_model=JobStatus)
async def get_synthesis_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobStatus(
        job_id=job_id,
        status=job["status"],
        date=job.get("date"),
        error=job.get("error"),
    )


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
