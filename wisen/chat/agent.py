"""
Wisen chat agent — answers questions about your coding sessions and reflections.

Backed by the WISEN_MODEL-configured model (see llm.py) with four tools:
  get_reflection        → fetch a specific day's Reflection from the local store
  list_reflections      → last 7 days of alignment scores
  search_chat_history  → grep raw session files across all coding agents
  search_agent_configs → search harness/config files (CLAUDE.md, MCP, skills…)

Designed to be called via POST /chat (SSE streaming).

On a stage-B server (wisen/cloud) the same agent answers from reflections
only (`raw_history=False`): the two search tools read the laptop's raw
history and configs, which never reach the server.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from dotenv import load_dotenv
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    UserPromptPart,
)
from pydantic_ai.settings import ModelSettings

from wisen.chat.search import search_agent_configs as _search_configs
from wisen.chat.search import search_chat_history as _search
from wisen.llm import get_model
from wisen.store import Store, get_store

load_dotenv()

_SYSTEM_PROMPT = """\
You are Wisen — a personal AI that watches the collaboration between this builder \
and their coding agents (Claude Code, Codex, Cursor, opencode, Gemini CLI, Pi), and \
helps them understand and improve it.

Your goal every turn: a grounded, specific answer about their actual sessions, \
reflections, and harness configs — never a generic one.

Your tools reach the source of record:
- get_reflection — one day's full reflection (moods, friction, recommendations)
- list_reflections — the last 7 days of scores, moods, and briefings
- search_chat_history — the raw transcripts across every coding agent
- search_agent_configs — the harness files (CLAUDE.md, AGENTS.md, MCP, skills, rules)

Working principles:
- Evidence before conclusions. When the answer depends on what actually happened — \
in a session, a config, a past day — retrieve it. The loaded context is a starting \
point, not the record. A claim you can't back with retrieved evidence is a guess; \
say so or go fetch.
- Own the ambiguity. Interpret vague ranges yourself ("lately", "this week" → the \
last 7 days) and fetch them. For fuzzy questions, take the most useful reading, \
answer it, and note the assumption in passing. Reserve clarifying questions for \
cases where a wrong guess would send you down a genuinely different path.
- Advice must be earned. For improvement or pattern questions, look across days \
before prescribing: which friction repeats, what changed after a config edit, which \
recommendations were ignored. Generic advice that could apply to anyone is a failure.
- Say what happened. You watched the sessions — be direct, name projects, dates, \
and incidents, and quote the moments that back each claim. Don't hedge, don't \
cheerlead. Concise unless asked to elaborate.
- Tools first, words after. A turn is either tool calls or your answer, never both: \
retrieve silently (the UI already shows progress), and write only once you have \
what you need — the text you write is the final answer.
"""

_REFLECTIONS_ONLY = """
Here you run on the builder's server, which holds reflections only: \
search_chat_history and search_agent_configs are not available, because raw \
transcripts and config files stay on their laptop. Answer from the reflections; \
when a question needs the raw sessions, say so.
"""


@dataclass
class ChatDeps:
    db: Store
    synthesis_context: str
    events: asyncio.Queue = field(default_factory=asyncio.Queue)


def _make_agent(raw_history: bool = True) -> Agent:
    model = get_model()
    # thinking=False keeps Gemini fast; other providers may not accept it
    settings = ModelSettings(thinking=False) if model.startswith("google:") else None
    agent = Agent(
        model=model,
        output_type=str,
        deps_type=ChatDeps,
        system_prompt=_SYSTEM_PROMPT if raw_history else _SYSTEM_PROMPT + _REFLECTIONS_ONLY,
        retries=1,
        model_settings=settings,
    )
    tools = (get_reflection, list_reflections) + ((search_chat_history, search_agent_configs) if raw_history else ())
    for fn in tools:
        agent.tool(fn)
    return agent


# Built lazily so importing the app doesn't require an API key.
_agents: dict[bool, Agent] = {}


def _get_agent(raw_history: bool = True) -> Agent:
    if raw_history not in _agents:
        _agents[raw_history] = _make_agent(raw_history)
    return _agents[raw_history]


def _resolve_date(raw: str) -> str:
    """
    Convert natural language date references to YYYY-MM-DD.
    Handles: 'yesterday', 'today', 'June 25', 'June 25th', '06-25', etc.
    Falls back to the raw string if unrecognised.
    """
    import re
    from datetime import timedelta

    raw = raw.strip().lower()
    # UTC to match how reflections and sessions are dated everywhere else
    today = datetime.now(UTC).date()

    if raw in ("today",):
        return today.isoformat()
    if raw in ("yesterday",):
        return (today - timedelta(days=1)).isoformat()

    # Already YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return raw

    # Month name patterns: "june 25", "june 25th", "jun 25"
    _MONTHS = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10,
        "november": 11, "december": 12,
    }
    m = re.match(r"([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s+(\d{4}))?", raw)
    if m:
        mon = _MONTHS.get(m.group(1))
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        if mon:
            return f"{year}-{mon:02d}-{day:02d}"

    return raw


async def get_reflection(ctx: RunContext[ChatDeps], date: str) -> dict:
    """
    Fetch the full Reflection for a specific date.
    date can be YYYY-MM-DD, or natural language like 'yesterday', 'June 25', 'June 25th 2026'.
    Returns mood, alignment score, friction points, recommendations, and synthesis_context.
    """
    resolved = _resolve_date(date)
    await ctx.deps.events.put({"type": "tool_call", "name": "get_reflection", "label": f"Looking up reflection for {resolved}"})
    doc = await ctx.deps.db.get_reflection(resolved)
    if not doc:
        available = await ctx.deps.db.list_reflections(limit=14)
        dates = [d["date"] for d in available]
        await ctx.deps.events.put({"type": "tool_result", "name": "get_reflection", "summary": "not found"})
        return {"error": f"No reflection for {resolved}", "available_dates": dates}
    alignment = doc.get("alignment_score")
    summary = f"alignment {alignment:.0f}" if isinstance(alignment, (int, float)) else "loaded"
    await ctx.deps.events.put({"type": "tool_result", "name": "get_reflection", "summary": summary})
    await ctx.deps.events.put({"type": "status", "text": "Reading through your session data..."})
    return doc


async def list_reflections(ctx: RunContext[ChatDeps]) -> list[dict]:
    """
    List the last 7 reflections — date, alignment_score, alignment_label, your_mood, agent_mood.
    Use this to answer trend questions or "how was my week".
    """
    await ctx.deps.events.put({"type": "tool_call", "name": "list_reflections", "label": "Loading last 7 days"})
    fields = ("date", "alignment_score", "alignment_label", "your_mood", "agent_mood", "synthesis_context")
    docs = [
        {k: d[k] for k in fields if k in d}
        for d in await ctx.deps.db.list_reflections(limit=7)
    ]
    count = len(docs)
    await ctx.deps.events.put({"type": "tool_result", "name": "list_reflections", "summary": f"{count} day{'s' if count != 1 else ''}"})
    await ctx.deps.events.put({"type": "status", "text": "Looking for patterns across the week..."})
    return docs


async def search_chat_history(
    ctx: RunContext[ChatDeps],
    query: str,
    sources: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    """
    Search raw chat history files across all coding agents for a keyword or phrase.
    sources: filter to specific agents — "claude-code", "codex", "cursor", "opencode", "gemini-cli", "pi"
    date_from / date_to: YYYY-MM-DD bounds (both optional)
    Returns up to 20 matches with excerpt + surrounding context turns.
    """
    await ctx.deps.events.put({"type": "tool_call", "name": "search_chat_history", "label": f"Searching for \"{query}\""})
    result = await _search(query, sources=sources, date_from=date_from, date_to=date_to)
    count = len(result)
    summary = f"{count} session{'s' if count != 1 else ''} found" if count else "nothing found"
    await ctx.deps.events.put({"type": "tool_result", "name": "search_chat_history", "summary": summary})
    if count:
        await ctx.deps.events.put({"type": "status", "text": f"Reading through {count} matching session{'s' if count != 1 else ''}..."})
    return result


async def search_agent_configs(ctx: RunContext[ChatDeps], query: str) -> list[dict]:
    """
    Search harness/config files across all coding agents for a keyword:
    CLAUDE.md, AGENTS.md, GEMINI.md, .cursorrules, Claude Code skills and
    subagents, MCP server configs, settings.json, codex config/rules.
    Returns up to 20 matches with source, file_path, and excerpt.
    """
    await ctx.deps.events.put({"type": "tool_call", "name": "search_agent_configs", "label": f"Searching configs for \"{query}\""})
    result = await _search_configs(query)
    count = len(result)
    summary = f"{count} file{'s' if count != 1 else ''} found" if count else "nothing found"
    await ctx.deps.events.put({"type": "tool_result", "name": "search_agent_configs", "summary": summary})
    return result


# ── Message conversion ────────────────────────────────────────────────────────

def _to_history(messages: list[dict]) -> list[ModelMessage]:
    """Convert [{role, content}] frontend messages (all but last) to Pydantic AI history."""
    history: list[ModelMessage] = []
    model = get_model()
    for m in messages:
        if m["role"] == "user":
            history.append(ModelRequest(parts=[UserPromptPart(content=m["content"])]))
        else:
            history.append(ModelResponse(
                parts=[TextPart(content=m["content"])],
                timestamp=datetime.now(UTC),
                model_name=model,
            ))
    return history


# ── Public streaming interface ────────────────────────────────────────────────

async def stream_chat(
    messages: list[dict],
    context_date: str | None = None,
    *,
    store: Store | None = None,
    raw_history: bool = True,
) -> AsyncIterator[dict[str, Any]]:
    """
    Stream a chat response as typed event dicts.

    messages: full conversation [{role: "user"|"assistant", content: str}]
              The last message must be role="user" — that's the current turn.
    context_date: YYYY-MM-DD of the reflection to anchor context on.
                  Defaults to the most recent available log.
    store: where reflections are read (default: the local store).
    raw_history: False on the server: reflection tools only.

    Yields dicts: {"type": "token", "data": str}
                  {"type": "tool_call", "name": str, "label": str}
                  {"type": "tool_result", "name": str}
    """
    if not messages or messages[-1]["role"] != "user":
        raise ValueError("Last message must be role=user")

    db = store or get_store()

    if context_date:
        log_doc = await db.get_reflection(context_date)
    else:
        log_doc = next(iter(await db.list_reflections(limit=1)), None)
    synthesis_context = (
        log_doc.get("synthesis_context", "") if log_doc
        else "No reflection available yet."
    )

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    deps = ChatDeps(db=db, synthesis_context=synthesis_context, events=queue)

    preamble = f"## Your Reflection Context\n\n{synthesis_context}\n\n---\n\n"
    current_message = preamble + messages[-1]["content"] if not any(
        m["role"] == "assistant" for m in messages
    ) else messages[-1]["content"]

    history = _to_history(messages[:-1])

    async def run_agent() -> None:
        # agent.iter streams text from EVERY model response and keeps going
        # through tool calls — run_stream would stop at the first text part,
        # cutting the answer off when the model narrates before a tool call.
        agent = _get_agent(raw_history)
        emitted_text = False
        try:
            async with agent.iter(
                current_message,
                deps=deps,
                message_history=history,
            ) as run:
                async for node in run:
                    if not Agent.is_model_request_node(node):
                        continue
                    started_this_node = False
                    async with node.stream(run.ctx) as node_stream:
                        async for event in node_stream:
                            text = ""
                            if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                                text = event.part.content
                            elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                                text = event.delta.content_delta
                            if not text:
                                continue
                            if emitted_text and not started_this_node:
                                await queue.put({"type": "token", "data": "\n\n"})
                            started_this_node = True
                            emitted_text = True
                            await queue.put({"type": "token", "data": text})
        except (ValueError, RuntimeError) as e:
            await queue.put({"type": "error", "message": str(e)})
        finally:
            await queue.put(None)

    task = asyncio.create_task(run_agent())
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item
    finally:
        await task
