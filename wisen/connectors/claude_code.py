"""
Reads Claude Code session transcripts from ~/.claude/projects/.
Each project directory is named as the encoded cwd (slashes → dashes).
Each session is a JSONL file; we stream it line by line.

Example events (one JSON object per line):

  # Plain user message — content is a string
  {"type": "user", "sessionId": "7f5a…", "cwd": "/Users/me/proj",
   "timestamp": "2026-07-21T14:03:22.511Z",
   "message": {"role": "user", "content": "fix the failing test"}}

  # Assistant message — content is a LIST of typed blocks; tool_use
  # contributes only its name (args dropped)
  {"type": "assistant", "sessionId": "7f5a…", "timestamp": "…",
   "message": {"role": "assistant", "content": [
     {"type": "text", "text": "The fixture path moved. Fixing it."},
     {"type": "tool_use", "id": "toolu_01AB…", "name": "Edit", "input": {…}}]}}

  # Tool RESULT returns as a *user*-role event (that's how the API models
  # it) — kept, prefixed "[tool_result] …" so synthesis sees outcomes.
  # A rejection carries is_error + a harness marker (evals/miner greps it).
  {"type": "user", "sessionId": "7f5a…", "timestamp": "…",
   "message": {"role": "user", "content": [
     {"type": "tool_result", "tool_use_id": "toolu_01AB…",
      "content": [{"type": "text", "text": "File updated successfully."}]}]}}

  # Permission-mode switch — KEPT as a synthetic user turn ("[permission-mode]
  # switched to acceptEdits"): mode changes are a trust signal (acceptEdits =
  # trusting, plan = cautious). Consecutive re-logs of the same mode are deduped;
  # the first log is baseline, not a switch. No timestamp — reuses the previous turn's.
  {"type": "permission-mode", "permissionMode": "acceptEdits", "sessionId": "7f5a…"}

  # Harness bookkeeping — no conversational signal; dropped via _SKIP_TYPES
  {"type": "file-history-snapshot", "snapshot": {…}}   # pre-edit file copies (rewind)
  {"type": "ai-title", "title": "Fix failing test"}    # UI tab title
  {"type": "last-prompt", "prompt": "fix the…"}        # input-box cache (dupes real msg)
  {"type": "queue-operation", "operation": "enqueue"}  # queued-message plumbing
  {"type": "attachment", "attachment": {…}}            # pasted-file metadata

Note the outer "type" and inner "role" disagree on purpose (a tool result is
type=user); we trust message.role. sessionId groups lines after /resume.
"""
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from wisen import config
from wisen.ingestion.normalizer import (
    NormalizedSession,
    Turn,
    compute_metadata,
    truncate_content,
)

_CLAUDE_DIR = config.root() / ".claude" / "projects"

_SKIP_TYPES = {
    "file-history-snapshot", "attachment",
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
    last_ts = datetime.now(UTC)
    last_mode = None
    for e in events:
        etype = e.get("type")
        if etype in _SKIP_TYPES:
            continue

        # Permission-mode switches are a trust signal (acceptEdits = trusting,
        # plan = cautious) worth surfacing to the Observer. Claude Code re-logs
        # the current mode without a change, so only actual switches are kept.
        # The event has no timestamp — reuse the last turn's so it sorts into place.
        if etype == "permission-mode":
            mode = e.get("permissionMode", "")
            if mode and mode != last_mode:
                if last_mode is not None:
                    turns.append(Turn(
                        role="user",
                        content=f"[permission-mode] switched to {mode}",
                        timestamp=last_ts,
                    ))
                last_mode = mode
            continue

        msg = e.get("message", {})
        role = msg.get("role")
        content = msg.get("content", "")
        ts_str = e.get("timestamp", "")

        if role not in ("user", "assistant"):
            continue

        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError:
            ts = datetime.now(UTC)
        last_ts = ts

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
