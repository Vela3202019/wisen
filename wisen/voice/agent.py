"""
Wisen voice agent — runs as a LiveKit Agents worker process.

Start with:
    cd apps/api && .venv/bin/python -m voice.agent dev

Requires livekit-server running locally:
    livekit-server --dev
"""
import asyncio
import os
from datetime import UTC, datetime

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession, ConversationItemAddedEvent
from livekit.plugins import google

from wisen.db import get_db

load_dotenv()
os.environ.setdefault("GOOGLE_API_KEY", os.environ.get("GEMINI_API_KEY", ""))

LIVEKIT_URL = "ws://localhost:7880"
API_KEY = "devkey"
API_SECRET = "secret"

_INSTRUCTIONS = """\
You are Wisen — a voice AI that helps developers process and clean up the mess \
left behind by their coding agent sessions.

Your job is to act like REM sleep for a developer's messy agent history: \
turn raw, noisy session logs into signal. You help them figure out what to keep, \
what to discard, what went wrong, and how to set up their next session better.

You focus on two things:
1. Cleanup — helping the developer triage their agent history: dead ends to drop, \
decisions worth saving, context that should carry forward, and clutter that's just noise.
2. Reflecting — processing what happened so tomorrow's sessions start cleaner. \
You surface patterns: where agents got lost, where the developer had to re-explain things, \
where momentum stalled, and what a better handoff would have looked like.

Agents you know about: Claude Code, Codex, Cursor, opencode, Gemini CLI.

Voice rules:
- Keep responses to 1-3 sentences unless the user asks for more detail.
- Speak naturally, no bullet points or markdown.
- Be concrete — name specific patterns, not generalities.
- Don't start with filler like "Sure!" or "Great question!".
- When the developer describes a session, help them make a decision: keep it, drop it, \
  or distill it into something actionable.
"""


class WisenVoiceAgent(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=_INSTRUCTIONS)


server = AgentServer()


@server.rtc_session(agent_name="wisen-voice")
async def wisen_voice(ctx: agents.JobContext):
    session = AgentSession(
        llm=google.realtime.RealtimeModel(
            voice="Puck",
            temperature=0.8,
            instructions=_INSTRUCTIONS,
        )
    )

    db = get_db()
    room_name = ctx.room.name
    now = datetime.now(UTC)

    await db.voice_sessions.update_one(
        {"room_name": room_name},
        {"$setOnInsert": {"room_name": room_name, "started_at": now, "messages": []}},
        upsert=True,
    )

    @session.on("conversation_item_added")
    def _on_item(event: ConversationItemAddedEvent) -> None:
        msg = event.item
        text = msg.text_content if hasattr(msg, "text_content") else None
        role = getattr(msg, "role", None)
        if not text or role not in ("user", "assistant"):
            return
        asyncio.ensure_future(
            db.voice_sessions.update_one(
                {"room_name": room_name},
                {
                    "$push": {"messages": {"role": role, "content": text, "at": datetime.now(UTC)}},
                    "$set": {"updated_at": datetime.now(UTC)},
                },
            )
        )

    await session.start(room=ctx.room, agent=WisenVoiceAgent())

    await session.generate_reply(
        instructions=(
            "Greet the user in one short sentence. You're Wisen. "
            "Your job is to help them clean up their agent session history and figure out what's worth keeping."
        )
    )


if __name__ == "__main__":
    os.environ.setdefault("LIVEKIT_URL", LIVEKIT_URL)
    os.environ.setdefault("LIVEKIT_API_KEY", API_KEY)
    os.environ.setdefault("LIVEKIT_API_SECRET", API_SECRET)
    agents.cli.run_app(server)
