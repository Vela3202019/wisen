from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import uuid

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from wisen.db import get_db, ensure_indexes
from wisen.ingestion.pipeline import ingest_all
from wisen.synthesis.agent import synthesize
from wisen.synthesis.schema import Reflection
from wisen.chat.agent import stream_chat


@asynccontextmanager
async def lifespan(app: FastAPI):
    await ensure_indexes()
    yield


app = FastAPI(title="Wisen API", lifespan=lifespan)

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
    except Exception as e:
        _jobs[job_id] = {"status": "error", "date": date, "error": str(e)}


@app.post("/synthesize", response_model=JobStatus)
async def trigger_synthesis(req: SynthesizeRequest, background_tasks: BackgroundTasks):
    date = req.date or datetime.now(timezone.utc).date().isoformat()
    job_id = f"{date}-{datetime.now(timezone.utc).strftime('%H%M%S')}"
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
    db = get_db()
    docs = await db.reflections.find(
        {}, {"date": 1, "alignment_score": 1, "alignment_label": 1, "synthesized_at": 1}
    ).sort("date", -1).to_list(30)
    for d in docs:
        d.pop("_id", None)
    return docs


@app.get("/reflections/{date}", response_model=Reflection)
async def get_reflection(date: str):
    db = get_db()
    doc = await db.reflections.find_one({"date": date})
    if not doc:
        raise HTTPException(status_code=404, detail=f"No reflection for {date}")
    doc.pop("_id", None)
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
    db = get_db()
    now = datetime.now(timezone.utc)

    # Resolve or create session
    session_id = req.session_id
    if session_id:
        exists = await db.chat_sessions.find_one({"session_id": session_id}, {"_id": 1})
        if not exists:
            session_id = None
    if not session_id:
        session_id = str(uuid.uuid4())
        await db.chat_sessions.insert_one({
            "session_id": session_id,
            "kind": "text",
            "created_at": now,
            "updated_at": now,
            "messages": [],
        })

    # Save user message immediately
    user_msg = messages[-1]
    await db.chat_sessions.update_one(
        {"session_id": session_id},
        {"$push": {"messages": {**user_msg, "at": now}}, "$set": {"updated_at": now}},
    )

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
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            if assistant_tokens:
                full = "".join(assistant_tokens)
                await db.chat_sessions.update_one(
                    {"session_id": session_id},
                    {
                        "$push": {"messages": {"role": "assistant", "content": full, "at": datetime.now(timezone.utc)}},
                        "$set": {"updated_at": datetime.now(timezone.utc)},
                    },
                )
            yield f"data: {json.dumps({'done': True})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/chat/sessions")
async def list_chat_sessions(limit: int = 20):
    db = get_db()
    docs = await db.chat_sessions.find(
        {},
        {"session_id": 1, "kind": 1, "created_at": 1, "updated_at": 1, "messages": {"$slice": 1}},
    ).sort("updated_at", -1).limit(limit).to_list(limit)
    for d in docs:
        d.pop("_id", None)
        # Surface first user message as preview
        first = next((m for m in d.get("messages", []) if m.get("role") == "user"), None)
        d["preview"] = first["content"][:80] if first else ""
        d.pop("messages", None)
    return docs


@app.get("/chat/sessions/{session_id}")
async def get_chat_session(session_id: str):
    db = get_db()
    doc = await db.chat_sessions.find_one({"session_id": session_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Session not found")
    doc.pop("_id", None)
    return doc


@app.post("/voice/token")
async def voice_token():
    """Return a LiveKit JWT for the renderer to join the voice room."""
    try:
        from wisen.voice.token import create_participant_token, LIVEKIT_URL
    except ImportError:
        raise HTTPException(status_code=501, detail="voice extra not installed: pip install 'wisen[voice]'")
    token, room_name = create_participant_token()
    return {"token": token, "url": LIVEKIT_URL, "room": room_name}


_MODEL_KEY_ENV = {
    "google": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


@app.get("/health")
async def health():
    import os

    from wisen.llm import get_model

    status = {"gemini": False, "mongodb": False}
    try:
        db = get_db()
        await db.command("ping")
        status["mongodb"] = True
    except Exception:
        pass
    status["gemini"] = bool(os.getenv("GEMINI_API_KEY"))
    model = get_model()
    provider = model.split(":", 1)[0]
    status["model"] = model
    status["llm_key"] = bool(os.getenv(_MODEL_KEY_ENV.get(provider, ""), ""))
    return status
