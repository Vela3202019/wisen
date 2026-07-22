"""
Detect correction moments in raw claude-code transcripts and turn them into
eval cases. All detection is deterministic; only enrich() calls an LLM, to fill
agent_action and behavior_check.

Detectors:
  tool-rejection      user declined a tool call (harness marker in tool_result)
  interruption        user hit escape mid-turn ("[Request interrupted by user")
  explicit-correction user message opens with a correction marker (no/stop/revert/…)
  rephrase-storm      second user message within 60s (same signal as
                      ingestion.normalizer's user_rephrases, at moment level)
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from pydantic import BaseModel
from pydantic_ai import Agent

from connectors.claude_code import (
    _CLAUDE_DIR, _decode_project_path, _group_by_session, _parse_jsonl,
)
from ingestion.normalizer import truncate_content
from evals.cases import Case, ContextTurn, Detector, assign_split
from llm import get_model

TOOL_REJECTION_MARKER = "The user doesn't want to proceed with this tool use"
INTERRUPTION_MARKER = "[Request interrupted by user"
_CORRECTION_RE = re.compile(
    r"^(no\b|stop\b|wait\b|don'?t\b|revert\b|undo\b|that'?s not\b|not what i\b|wrong\b)",
    re.IGNORECASE,
)
# Harness-injected user messages (notifications, command wrappers) — not human turns.
_HARNESS_RE = re.compile(r"^\s*(<(task-notification|command-name|local-command|system-reminder|bash-)|Caveat:)")
_CONTEXT_TURNS = 12
_RAPID_CORRECTION_SECONDS = 60


@dataclass
class SessionScan:
    date: str
    session_id: str
    project_path: str
    source_path: str
    user_turns: int = 0
    cases: list[Case] = field(default_factory=list)


def _plain_text(content) -> str:
    """User-authored text only — excludes tool_result blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _tool_rejection_text(content) -> str:
    """Return the rejection tool_result text if present, else ''."""
    if not isinstance(content, list):
        return ""
    for b in content:
        if not (isinstance(b, dict) and b.get("type") == "tool_result"):
            continue
        inner = b.get("content", "")
        if isinstance(inner, list):
            inner = "\n".join(
                ib.get("text", "") for ib in inner
                if isinstance(ib, dict) and ib.get("type") == "text"
            )
        if isinstance(inner, str) and TOOL_REJECTION_MARKER in inner:
            return inner
    return ""


def _parse_ts(e: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(e.get("timestamp", "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _detect(text: str, rejection: str, prev_user_ts: datetime | None,
            ts: datetime | None) -> tuple[Detector, str] | None:
    """Classify one user turn. Returns (detector, correction_text) or None."""
    if rejection:
        return "tool-rejection", truncate_content(rejection, 500)
    if INTERRUPTION_MARKER in text:
        return "interruption", truncate_content(text, 500)
    if _CORRECTION_RE.match(text.strip()):
        return "explicit-correction", truncate_content(text, 500)
    if (text.strip() and prev_user_ts and ts
            and (ts - prev_user_ts).total_seconds() < _RAPID_CORRECTION_SECONDS):
        return "rephrase-storm", truncate_content(text, 500)
    return None


def _scan_session(session_id: str, events: list[dict], project_path: str,
                  source_path: str) -> SessionScan | None:
    # The decoded dir name is lossy (dashes vs path separators); events carry
    # the real cwd — required for replay to find the project's git repo.
    project_path = next(
        (e["cwd"] for e in events if e.get("cwd")), project_path,
    )
    # Simplified ordered turns: (event_index, role, display_text, ts, rejection_text)
    turns = []
    for i, e in enumerate(events):
        if e.get("isMeta") or e.get("isSidechain"):
            continue
        msg = e.get("message", {})
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content", "")
        text = _plain_text(content)
        rejection = _tool_rejection_text(content) if role == "user" else ""
        if not text.strip() and not rejection:
            continue
        if role == "user" and _HARNESS_RE.match(text):
            continue
        turns.append((i, role, text, _parse_ts(e), rejection))

    if not turns:
        return None
    first_ts = next((t[3] for t in turns if t[3]), None)
    if first_ts is None:
        return None

    date = first_ts.date().isoformat()
    scan = SessionScan(
        date=date, session_id=session_id,
        project_path=project_path, source_path=source_path,
    )

    prev_user_ts = None
    for idx, (event_i, role, text, ts, rejection) in enumerate(turns):
        if role != "user":
            continue
        scan.user_turns += 1
        hit = _detect(text, rejection, prev_user_ts, ts)
        prev_user_ts = ts or prev_user_ts
        if not hit:
            continue
        detector, correction = hit
        context = [
            ContextTurn(role=r, content=truncate_content(t or rej, 1500))
            for _, r, t, _, rej in turns[max(0, idx - _CONTEXT_TURNS):idx]
        ]
        scan.cases.append(Case(
            id=f"{date}-{session_id[:8]}-{event_i}",
            date=date, session_id=session_id,
            source_path=source_path, project_path=project_path,
            detector=detector, context=context, correction=correction,
            split=assign_split(date),
        ))
    return scan


def scan_all(claude_dir: Path = _CLAUDE_DIR, since: str | None = None) -> Iterator[SessionScan]:
    """Walk every claude-code transcript and yield per-session scan results."""
    if not claude_dir.exists():
        return
    for project_dir in sorted(claude_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        project_path = _decode_project_path(project_dir.name)
        for jsonl_file in sorted(project_dir.glob("*.jsonl")):
            events = _parse_jsonl(jsonl_file)
            for session_id, session_events in _group_by_session(events).items():
                scan = _scan_session(session_id, session_events, project_path, str(jsonl_file))
                if scan is None or (since and scan.date < since):
                    continue
                yield scan


# ── LLM enrichment (the only non-deterministic step) ─────────────────────────

class Enrichment(BaseModel):
    agent_action: str    # one sentence: what the agent did or was about to do
    behavior_check: str  # yes/no question a judge can answer from a replay transcript


_ENRICH_PROMPT = """You are labeling an eval case mined from a real coding-agent session.
The user corrected the agent. From the context and the correction, produce:
- agent_action: one sentence describing what the agent did or was about to do that got corrected.
- behavior_check: a single yes/no question that, asked about a fresh replay transcript of the
  same context, detects whether the agent repeated the corrected behavior.
  Example: "Did the agent add a fallback default value the user didn't ask for?"
Be specific to this case; never generic."""

_enricher = Agent(get_model(), output_type=Enrichment, instructions=_ENRICH_PROMPT)


async def enrich(case: Case) -> Case:
    transcript = "\n".join(f"{t.role}: {t.content}" for t in case.context)
    result = await _enricher.run(
        f"## Context (before the correction)\n{transcript}\n\n"
        f"## Detector\n{case.detector}\n\n## User's correction\n{case.correction}"
    )
    case.agent_action = result.output.agent_action
    case.behavior_check = result.output.behavior_check
    return case
