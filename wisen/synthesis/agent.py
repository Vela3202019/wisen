"""
Two-stage synthesis pipeline:
  Stage 1 — Observer: reads raw sessions + configs + yesterday's Reflection → DayObservation
  Stage 2 — Synthesizer: reads DayObservation + past 7 days → Reflection (saved to MongoDB)
"""
import asyncio
from datetime import datetime, timezone

from pydantic import BaseModel
from pydantic_ai import Agent
from dotenv import load_dotenv

from wisen.db import get_db
from wisen.llm import get_model
from wisen.ingestion.pipeline import IngestResult
from wisen.ingestion.normalizer import NormalizedSession
from wisen.connectors.agent_configs import AgentConfig
from wisen.synthesis.schema import DayObservation, Reflection
from wisen.synthesis.prompt import OBSERVER_PROMPT, SYNTHESIZER_PROMPT
from wisen.synthesis.claude_cli import is_cli_model, run_structured

load_dotenv()


# ── Formatters ────────────────────────────────────────────────────────────────

def _format_session(s: NormalizedSession) -> str:
    lines = [
        f"[{s.source}] {s.project_name} — {s.date}",
        f"Duration: {s.metadata.session_length_minutes:.0f}min  "
        f"Turns: {s.metadata.total_turns}",
    ]
    for t in s.turns:
        prefix = "You" if t.role == "user" else "Agent"
        lines.append(f"{prefix}: {t.content[:400]}")
    return "\n".join(lines)


def _format_config(c: AgentConfig) -> str:
    return f"[{c.source}] {c.path}\n{c.content[:1000]}"


def _format_yesterday(doc: dict | None) -> str:
    if not doc:
        return "## Yesterday's Reflection\n\nNo baseline — this is Day 1."
    lines = [
        f"## Yesterday's Reflection ({doc.get('date', 'unknown')})",
        f"Alignment: {doc.get('alignment_score', '?')} ({doc.get('alignment_label', '?')})",
        f"Your mood: {doc.get('your_mood', {}).get('label', '?')}",
        f"Agent mood: {doc.get('agent_mood', {}).get('label', '?')}",
    ]
    fps = doc.get("friction_points", [])
    if fps:
        lines.append("Friction points:")
        for fp in fps:
            lines.append(f"  [{fp.get('type')}] {fp.get('description')}")
    recs = doc.get("recommendations", [])
    if recs:
        lines.append("Recommendations given:")
        for r in recs:
            lines.append(f"  → [{r.get('target')}] {r.get('action')}")
    acted = doc.get("acted_on_recommendations", [])
    if acted:
        lines.append("Acted on (from day before):")
        for a in acted:
            lines.append(f"  ✓ {a}")
    return "\n".join(lines)


# ── Context builders ──────────────────────────────────────────────────────────

def _build_observer_context(
    result: IngestResult,
    date: str,
    yesterday_doc: dict | None,
) -> str:
    sections = []

    day_sessions = [s for s in result.sessions if s.source != "git" and s.date == date]
    if day_sessions:
        text = "\n\n---\n\n".join(_format_session(s) for s in day_sessions)
        sections.append(f"## Chat Sessions\n\n{text}")

    git_sessions = [s for s in result.sessions if s.source == "git" and s.date == date]
    if git_sessions:
        text = "\n\n".join(_format_session(s) for s in git_sessions)
        sections.append(f"## Git Commits\n\n{text}")

    configs_text = "\n\n".join(_format_config(c) for c in result.configs[:20])
    if configs_text:
        sections.append(f"## Agent Configs\n\n{configs_text}")

    sections.append(_format_yesterday(yesterday_doc))

    return "\n\n".join(sections)


def _build_synthesizer_context(
    observation: DayObservation,
    yesterday_doc: dict | None,
    past_docs: list[dict],
    date: str,
) -> str:
    obs_json = observation.model_dump_json(indent=2)

    history_lines = []
    for d in past_docs:
        history_lines.append(
            f"{d['date']}: score={d.get('alignment_score', '?')} "
            f"label={d.get('alignment_label', '?')}"
        )

    yesterday_recs = []
    if yesterday_doc:
        yesterday_recs = [
            f"[{r.get('target')}] {r.get('action')}"
            for r in yesterday_doc.get("recommendations", [])
        ]

    parts = [
        f"Synthesize the Reflection for {date}.",
        f"\n## DayObservation (from Observer agent)\n\n```json\n{obs_json}\n```",
    ]
    if yesterday_recs:
        parts.append("## Yesterday's Recommendations\n\n" + "\n".join(f"- {r}" for r in yesterday_recs))
    if history_lines:
        parts.append("## Past 7 Days\n\n" + "\n".join(history_lines))

    return "\n\n".join(parts)


# ── Generation ────────────────────────────────────────────────────────────────

async def _generate(
    system_prompt: str, user_prompt: str, output_type: type[BaseModel], model: str
) -> BaseModel:
    """One structured generation — via `claude -p` on the subscription when
    WISEN_SYNTH_MODEL=claude-cli:*, otherwise via pydantic-ai."""
    if is_cli_model():
        return await asyncio.to_thread(run_structured, system_prompt, user_prompt, output_type)
    agent = Agent(model=model, output_type=output_type, system_prompt=system_prompt, retries=2)
    return (await agent.run(user_prompt)).output


# ── Main entry point ──────────────────────────────────────────────────────────

async def synthesize(result: IngestResult, date: str | None = None) -> Reflection:
    target_date = date or datetime.now(timezone.utc).date().isoformat()

    db = get_db()

    # Yesterday's full Reflection for diff analysis
    yesterday_doc = await db.reflections.find_one(
        {"date": {"$lt": target_date}},
        sort=[("date", -1)],
    )

    # Past 7 days (lightweight) for trend
    past_docs = await db.reflections.find(
        {"date": {"$lt": target_date}},
        {"date": 1, "alignment_score": 1, "alignment_label": 1},
    ).sort("date", -1).limit(7).to_list(7)

    model = get_model()

    # ── Stage 1: Observer ─────────────────────────────────────────────────────
    print("  [Stage 1] Observer reading sessions + configs + yesterday's reflection...")
    observer_context = _build_observer_context(result, target_date, yesterday_doc)
    observation = await _generate(
        OBSERVER_PROMPT,
        f"Observe and analyze {target_date}.\n\n{observer_context}",
        DayObservation,
        model,
    )
    print(f"  [Stage 1] Done. Topics: {observation.topics_raw}")

    # ── Stage 2: Synthesizer ──────────────────────────────────────────────────
    print("  [Stage 2] Synthesizer producing Reflection...")
    synth_context = _build_synthesizer_context(observation, yesterday_doc, past_docs, target_date)
    reflection = await _generate(SYNTHESIZER_PROMPT, synth_context, Reflection, model)
    reflection.date = target_date
    print(f"  [Stage 2] Done. Alignment: {reflection.alignment_score:.2f} ({reflection.alignment_label})")

    # ── Save to MongoDB ───────────────────────────────────────────────────────
    doc = reflection.model_dump()
    doc["synthesized_at"] = datetime.now(timezone.utc)
    await db.reflections.update_one(
        {"date": target_date},
        {"$set": doc},
        upsert=True,
    )

    return reflection
