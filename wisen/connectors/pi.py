"""
Pi connector. Reads two unrelated products that share the name "Pi":

1. Pi the coding agent (github.com/earendil-works/pi) — local session
   transcripts under ~/.pi/agent/sessions/--<encoded cwd>--/<ts>_<id>.jsonl.
2. Pi the chatbot (pi.ai) — web-only, no local storage; read from exports
   dropped into ~/.wisen/exports/pi/ (override with PI_EXPORT_DIR).

Both yield source="pi".

── 1. Pi coding-agent sessions ──
Format: packages/coding-agent/docs/session-format.md in the pi repo. Each line
is an entry; entries form a tree via id/parentId so one file can hold several
branches. We keep only the active branch: walk parentId from the leaf (the last
entry appended) back to the root.

  # Header — first line, not part of the tree
  {"type": "session", "version": 3, "id": "uuid", "cwd": "/Users/me/proj",
   "timestamp": "2026-07-21T09:12:01.000Z"}

  # Messages — entry timestamp is ISO; role is user | assistant | toolResult
  # (system messages carry the prompt/tool loadout and are dropped)
  {"type": "message", "id": "a1b2c3d4", "parentId": "prev1234", "timestamp": "…",
   "message": {"role": "user", "content": "fix the failing test"}}
  {"type": "message", "id": "b2c3d4e5", "parentId": "a1b2c3d4", "timestamp": "…",
   "message": {"role": "assistant", "content": [
     {"type": "text", "text": "Fixing it."},
     {"type": "toolCall", "id": "call_1", "name": "bash", "arguments": {…}}]}}
  # Tool result — kept as a user turn prefixed "[tool_result] …", like claude_code
  {"type": "message", "id": "c3d4e5f6", "parentId": "b2c3d4e5", "timestamp": "…",
   "message": {"role": "toolResult", "toolName": "bash",
     "content": [{"type": "text", "text": "1 passed"}], "isError": false}}

  # Compaction — Pi keeps the full history in the file, so the summary is
  # dropped: the summarized entries are already on the path and count once
  {"type": "compaction", "id": "f6g7h8i9", "parentId": "e5f6g7h8", "timestamp": "…",
   "summary": "User discussed X…", "firstKeptEntryId": "c3d4e5f6"}

  # compaction, model_change, thinking_level_change, usage, label, session_info, custom,
  # custom_message, branch_summary, context_edit — no conversational turn; dropped

── 2. pi.ai exports ──
How to export from Pi:
  1. Go to pi.ai → Settings → Privacy → Download your data
  2. Pi sends a ZIP containing conversations.json
  3. Extract and drop conversations.json into ~/.wisen/exports/pi/

Supported export formats:
  A. Pi official export — conversations.json:
     [{ "id", "title", "messages": [{ "role", "content", "created_at" }] }]

  B. Simple JSON paste — any file named pi-*.json in the exports folder:
     { "turns": [{ "role", "content", "timestamp?" }] }

  C. Plain text paste — pi-*.txt:
     Alternating lines "You: ..." / "Pi: ..."
"""
import json
import os
import re
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

_DEFAULT_PI_EXPORT_DIR = config.root() / ".wisen" / "exports" / "pi"
_PI_SESSIONS_DIR = config.root() / ".pi" / "agent" / "sessions"

_ROLE_MAP = {
    "human": "user",
    "user": "user",
    "assistant": "assistant",
    "pi": "assistant",
    "ai": "assistant",
}


def _parse_ts(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return datetime.now(UTC)


def _from_official_export(data: list) -> list[NormalizedSession]:
    """Pi official conversations.json export."""
    sessions = []
    for conv in data:
        if not isinstance(conv, dict):
            continue
        messages = conv.get("messages", [])
        turns = []
        for m in messages:
            role = _ROLE_MAP.get(m.get("role", ""), None)
            if role is None:
                continue
            text = truncate_content(str(m.get("content", "")).strip())
            if not text:
                continue
            ts = _parse_ts(m.get("created_at") or m.get("timestamp"))
            turns.append(Turn(role=role, content=text, timestamp=ts))

        if not turns:
            continue
        turns.sort(key=lambda t: t.timestamp)
        date = turns[0].timestamp.date().isoformat()
        conv_id = conv.get("id") or f"{hash(turns[0].content) & 0xFFFFFF:06x}"
        sessions.append(NormalizedSession(
            session_id=f"pi:{conv_id}",
            source="pi",
            project_path="",
            project_name=conv.get("title", "Pi conversation"),
            date=date,
            started_at=turns[0].timestamp,
            ended_at=turns[-1].timestamp,
            turns=turns,
            metadata=compute_metadata(turns),
            raw_source_path="pi-export",
        ))
    return sessions


def _from_simple_json(data: dict, filename: str) -> NormalizedSession | None:
    """{ turns: [{role, content, timestamp?}] } format."""
    raw_turns = data.get("turns", data.get("messages", []))
    turns = []
    for t in raw_turns:
        role = _ROLE_MAP.get(t.get("role", ""), None)
        if role is None:
            continue
        text = truncate_content(str(t.get("content", "")).strip())
        if not text:
            continue
        ts = _parse_ts(t.get("timestamp") or t.get("created_at"))
        turns.append(Turn(role=role, content=text, timestamp=ts))

    if not turns:
        return None
    turns.sort(key=lambda t: t.timestamp)
    date = turns[0].timestamp.date().isoformat()
    return NormalizedSession(
        session_id=f"pi:{Path(filename).stem}",
        source="pi",
        project_path="",
        project_name=Path(filename).stem,
        date=date,
        started_at=turns[0].timestamp,
        ended_at=turns[-1].timestamp,
        turns=turns,
        metadata=compute_metadata(turns),
        raw_source_path=filename,
    )


def _from_plain_text(text: str, filename: str) -> NormalizedSession | None:
    """
    Parse alternating 'You: ...' / 'Pi: ...' plain text.
    Also handles unlabeled alternating lines (assumes user first).
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    turns = []
    ts_base = datetime.now(UTC)
    from datetime import timedelta

    you_pattern = re.compile(r"^(you|me|human)\s*:\s*", re.IGNORECASE)
    pi_pattern = re.compile(r"^(pi|assistant|ai)\s*:\s*", re.IGNORECASE)

    has_labels = any(you_pattern.match(l) or pi_pattern.match(l) for l in lines)

    for i, line in enumerate(lines):
        if has_labels:
            if you_pattern.match(line):
                role = "user"
                content = you_pattern.sub("", line).strip()
            elif pi_pattern.match(line):
                role = "assistant"
                content = pi_pattern.sub("", line).strip()
            else:
                continue
        else:
            role = "user" if i % 2 == 0 else "assistant"
            content = line

        content = truncate_content(content)
        if not content:
            continue
        turns.append(Turn(
            role=role,
            content=content,
            timestamp=ts_base + timedelta(seconds=i * 30),
        ))

    if not turns:
        return None
    date = turns[0].timestamp.date().isoformat()
    return NormalizedSession(
        session_id=f"pi:{Path(filename).stem}",
        source="pi",
        project_path="",
        project_name=Path(filename).stem,
        date=date,
        started_at=turns[0].timestamp,
        ended_at=turns[-1].timestamp,
        turns=turns,
        metadata=compute_metadata(turns),
        raw_source_path=filename,
    )


def _decode_project_path(encoded: str) -> str:
    """'--Users-me-projects-foo--' → '/Users/me/projects/foo' (lossy: '-' in names)."""
    return "/" + encoded.strip("-").replace("-", "/")


def _active_branch(entries: list[dict]) -> list[dict]:
    """Root→leaf path of the tree."""
    by_id = {e["id"]: e for e in entries if e.get("id")}
    path: list[dict] = []
    node = entries[-1] if entries else None
    while node is not None and len(path) <= len(by_id):  # bound guards a parentId cycle
        path.append(node)
        node = by_id.get(node.get("parentId"))
    path.reverse()
    return path


def _block_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _session_turns(path: list[dict]) -> list[Turn]:
    turns = []
    for e in path:
        if e.get("type") == "message":
            msg = e.get("message") or {}
            content = msg.get("content")
            role = msg.get("role")
            tool_names = []
            if role == "user":
                text = _block_text(content)
            elif role == "assistant":
                text = _block_text(content)
                tool_names = [
                    b["name"] for b in content or []
                    if isinstance(b, dict) and b.get("type") == "toolCall" and "name" in b
                ]
            elif role == "toolResult":
                role, text = "user", f"[tool_result] {_block_text(content)}"
            else:
                continue  # system prompt/tool loadout, bashExecution, custom, …
        else:
            continue

        text = truncate_content(text.strip())
        if not text:
            continue
        turns.append(Turn(
            role=role,
            content=text,
            timestamp=_parse_ts(e.get("timestamp")),
            has_tool_call=bool(tool_names),
            tool_names=tool_names,
        ))
    return turns


def _parse_session_file(path: Path, project_dir: Path) -> NormalizedSession | None:
    entries: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
    except OSError:
        return None

    header = next((e for e in entries if e.get("type") == "session"), {})
    tree = [e for e in entries if e.get("type") != "session"]
    turns = _session_turns(_active_branch(tree))
    if not turns:
        return None

    cwd = header.get("cwd") or _decode_project_path(project_dir.name)
    session_id = header.get("id") or path.stem
    return NormalizedSession(
        session_id=f"pi:{cwd}:{session_id}",
        source="pi",
        project_path=cwd,
        project_name=Path(cwd).name,
        date=turns[0].timestamp.date().isoformat(),
        started_at=turns[0].timestamp,
        ended_at=turns[-1].timestamp,
        turns=turns,
        metadata=compute_metadata(turns),
        raw_source_path=str(path),
    )


async def _ingest_sessions(date: str | None) -> AsyncIterator[NormalizedSession]:
    if not _PI_SESSIONS_DIR.exists():
        return
    for project_dir in _PI_SESSIONS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl_file in project_dir.glob("*.jsonl"):
            session = _parse_session_file(jsonl_file, project_dir)
            if session and (not date or session.date == date):
                yield session


async def _ingest_exports(
    export_dir: str | None,
    date: str | None,
) -> AsyncIterator[NormalizedSession]:
    root = Path(export_dir) if export_dir else Path(
        os.getenv("PI_EXPORT_DIR", str(_DEFAULT_PI_EXPORT_DIR))
    )
    if not root.exists():
        return

    for f in root.rglob("*"):
        if not f.is_file():
            continue

        sessions: list[NormalizedSession] = []

        if f.suffix == ".json":
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

            # Official Pi export: top-level list of conversations
            if isinstance(data, list):
                sessions = _from_official_export(data)
            elif isinstance(data, dict):
                s = _from_simple_json(data, str(f))
                if s:
                    sessions = [s]

        elif f.suffix == ".txt":
            try:
                s = _from_plain_text(f.read_text(encoding="utf-8"), str(f))
                if s:
                    sessions = [s]
            except OSError:
                continue

        for session in sessions:
            if date and session.date != date:
                continue
            yield session


async def ingest(
    export_dir: str | None = None,
    date: str | None = None,
) -> AsyncIterator[NormalizedSession]:
    async for session in _ingest_sessions(date):
        yield session
    async for session in _ingest_exports(export_dir, date):
        yield session
