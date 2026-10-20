"""
Two-stage synthesis pipeline:
  Stage 1 — Observer: reads raw sessions + configs + yesterday's Reflection → DayObservation
  Stage 2 — Synthesizer: reads DayObservation + past 7 days → Reflection (saved to the local store)
"""
import asyncio
from datetime import UTC, datetime

from dotenv import load_dotenv
from pydantic import BaseModel
from pydantic_ai import Agent

from wisen.connectors.agent_configs import AgentConfig
from wisen.ingestion.normalizer import NormalizedSession
from wisen.ingestion.pipeline import IngestResult
from wisen.llm import get_model
from wisen.store import get_store
from wisen.synthesis.claude_cli import is_cli_model, run_structured
from wisen.synthesis.prompt import OBSERVER_PROMPT, SYNTHESIZER_PROMPT
from wisen.synthesis.schema import DayObservation, Reflection

load_dotenv()


# ── Formatters ────────────────────────────────────────────────────────────────

def _format_session(s: NormalizedSession) -> str:
    lines = [
        f"[{s.source}] {s.project_name} — {s.date}",
        (f"Duration: {s.metadata.session_length_minutes:.0f}min  "
         f"Turns: {s.metadata.total_turns}"),
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
    ignored = doc.get("ignored_recommendations", [])
    if ignored:
        lines.append("Still not acted on (from day before):")
        for i in ignored:
            lines.append(f"  ✗ {i}")
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


# ── Stages (each one checkpointed separately by the synthesis job) ────────────

async def observe(result: IngestResult, date: str, yesterday_doc: dict | None, model: str) -> DayObservation:
    """Stage 1: Observer."""
    print("  [Stage 1] Observer reading sessions + configs + yesterday's reflection...")
    observer_context = _build_observer_context(result, date, yesterday_doc)
    observation = await _generate(
        OBSERVER_PROMPT,
        f"Observe and analyze {date}.\n\n{observer_context}",
        DayObservation,
        model,
    )
    print(f"  [Stage 1] Done. Topics: {observation.topics_raw}")
    return observation


async def reflect(
    observation: DayObservation, yesterday_doc: dict | None, past_docs: list[dict], date: str, model: str
) -> Reflection:
    """Stage 2: Synthesizer."""
    print("  [Stage 2] Synthesizer producing Reflection...")
    synth_context = _build_synthesizer_context(observation, yesterday_doc, past_docs, date)
    reflection = await _generate(SYNTHESIZER_PROMPT, synth_context, Reflection, model)
    reflection.date = date
    print(f"  [Stage 2] Done. Alignment: {reflection.alignment_score:.2f} ({reflection.alignment_label})")
    return reflection


async def save_reflection(reflection: Reflection) -> None:
    """Upsert by date, so saving the same day twice leaves one reflection."""
    doc = reflection.model_dump()
    doc["synthesized_at"] = datetime.now(UTC)
    await get_store().upsert_reflection(doc)


async def load_history(date: str) -> tuple[dict | None, list[dict]]:
    """Yesterday's full Reflection (for diff analysis) and the past 7 days (for trend)."""
    past_docs = await get_store().list_reflections(limit=7, before=date)
    return (past_docs[0] if past_docs else None), past_docs


# ── Main entry point ──────────────────────────────────────────────────────────

async def synthesize(result: IngestResult, date: str | None = None) -> Reflection:
    target_date = date or datetime.now(UTC).date().isoformat()
    yesterday_doc, past_docs = await load_history(target_date)
    model = get_model()
    observation = await observe(result, target_date, yesterday_doc, model)
    reflection = await reflect(observation, yesterday_doc, past_docs, target_date, model)
    await save_reflection(reflection)
    return reflection
