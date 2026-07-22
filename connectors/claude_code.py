"""
Reads Claude Code session transcripts from ~/.claude/projects/.
Each project directory is named as the encoded cwd (slashes → dashes).
Each session is a JSONL file; we stream it line by line.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from ingestion.normalizer import (
    NormalizedSession, Turn, compute_metadata, truncate_content
)

_CLAUDE_DIR = Path.home() / ".claude" / "projects"

_SKIP_TYPES = {
    "permission-mode", "file-history-snapshot", "attachment",
    "ai-title", "last-prompt", "queue-operation",
}


def _decode_project_path(encoded: str) -> str:
    """'-Users-me-projects-foo' → '/Users/me/projects/foo'"""
    return "/" + encoded.lstrip("-").replace("-", "/")


def _extract_text(content) -> str:
    """Pull plain text from a message content field (str or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    # Include tool result text so the synthesis agent can see outcomes
                    inner = block.get("content", "")
                    if isinstance(inner, list):
                        for ib in inner:
                            if isinstance(ib, dict) and ib.get("type") == "text":
                                parts.append(f"[tool_result] {ib.get('text', '')}")
                    elif isinstance(inner, str):
                        parts.append(f"[tool_result] {inner}")
        return "\n".join(parts)
    return ""


def _extract_tool_names(content) -> list[str]:
    if not isinstance(content, list):
        return []
    return [
        b["name"] for b in content
        if isinstance(b, dict) and b.get("type") == "tool_use" and "name" in b
    ]


def _parse_jsonl(path: Path) -> list[dict]:
    events = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # skip corrupt lines
    return events


def _group_by_session(events: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for e in events:
        sid = e.get("sessionId", "unknown")
        groups.setdefault(sid, []).append(e)
    return groups


def _events_to_turns(events: list[dict]) -> list[Turn]:
    turns = []
    for e in events:
        etype = e.get("type")
        if etype in _SKIP_TYPES:
            continue

        msg = e.get("message", {})
        role = msg.get("role")
        content = msg.get("content", "")
        ts_str = e.get("timestamp", "")

        if role not in ("user", "assistant"):
            continue

        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            ts = datetime.now(timezone.utc)

        text = truncate_content(_extract_text(content))
        if not text.strip():
            continue

        tool_names = _extract_tool_names(content) if role == "assistant" else []

        turns.append(Turn(
            role=role,
            content=text,
            timestamp=ts,
            has_tool_call=bool(tool_names),
            tool_names=tool_names,
        ))

    return sorted(turns, key=lambda t: t.timestamp)


async def ingest(date: str | None = None) -> AsyncIterator[NormalizedSession]:
    """
    Yield NormalizedSession for every Claude Code session.
    If date is given, only yield sessions from that date (YYYY-MM-DD).
    """
    if not _CLAUDE_DIR.exists():
        return

    for project_dir in _CLAUDE_DIR.iterdir():
        if not project_dir.is_dir():
            continue

        project_path = _decode_project_path(project_dir.name)
        project_name = Path(project_path).name

        for jsonl_file in project_dir.glob("*.jsonl"):
            events = _parse_jsonl(jsonl_file)
            if not events:
                continue

            for session_id, session_events in _group_by_session(events).items():
                turns = _events_to_turns(session_events)
                if not turns:
                    continue

                session_date = turns[0].timestamp.date().isoformat()
                if date and session_date != date:
                    continue

                metadata = compute_metadata(turns)
                session = NormalizedSession(
                    session_id=f"claude-code:{project_path}:{session_id}",
                    source="claude-code",
                    project_path=project_path,
                    project_name=project_name,
                    date=session_date,
                    started_at=turns[0].timestamp,
                    ended_at=turns[-1].timestamp,
                    turns=turns,
                    metadata=metadata,
                    raw_source_path=str(jsonl_file),
                )
                yield session
