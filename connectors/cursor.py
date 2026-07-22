"""
Reads Cursor chat sessions from workspace SQLite databases.
Each workspace at ~/Library/Application Support/Cursor/User/workspaceStorage/[id]/state.vscdb
has an ItemTable (key/value) and cursorDiskKV table containing chat history.
"""
import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from ingestion.normalizer import (
    NormalizedSession, Turn, compute_metadata, truncate_content
)

_WORKSPACE_ROOT = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Cursor"
    / "User"
    / "workspaceStorage"
)

# Keys Cursor uses to store chat history
_CHAT_KEYS = {
    "aichat.workspaceState.chat",
    "workbench.panel.aichat",
    "composer.composerData",
}


def _open_db(path: Path):
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except Exception:
        return None


def _extract_chats_from_db(db_path: Path) -> list[dict]:
    conn = _open_db(db_path)
    if conn is None:
        return []

    chats = []
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Try ItemTable first
        try:
            cur.execute("SELECT key, value FROM ItemTable")
            for row in cur.fetchall():
                key = row["key"]
                if any(k in key for k in ("aichat", "composer", "chat")):
                    try:
                        value = json.loads(row["value"])
                        if isinstance(value, dict):
                            chats.append({"key": key, "value": value, "source": "ItemTable"})
                    except Exception:
                        pass
        except Exception:
            pass

        # Try cursorDiskKV
        try:
            cur.execute("SELECT key, value FROM cursorDiskKV")
            for row in cur.fetchall():
                key = row["key"]
                try:
                    value = json.loads(row["value"])
                    if isinstance(value, dict) and (
                        "conversation" in value
                        or "messages" in value
                        or "tabs" in value
                    ):
                        chats.append({"key": key, "value": value, "source": "cursorDiskKV"})
                except Exception:
                    pass
        except Exception:
            pass

    finally:
        conn.close()

    return chats


def _parse_chat_blob(blob: dict, db_path: Path) -> list[NormalizedSession]:
    """
    Cursor stores chats in various shapes depending on version.
    Try multiple known shapes.
    """
    sessions = []
    value = blob["value"]

    # Shape 1: { tabs: [{ id, title, conversation: [{role, content, ...}] }] }
    tabs = value.get("tabs", [])
    for tab in tabs:
        conv = tab.get("conversation", [])
        turns = _conv_to_turns(conv)
        if turns:
            sessions.append(_make_session(turns, tab.get("title", "Cursor chat"), db_path))

    # Shape 2: { conversation: [...] }
    conv = value.get("conversation", [])
    if conv and not tabs:
        turns = _conv_to_turns(conv)
        if turns:
            sessions.append(_make_session(turns, "Cursor chat", db_path))

    # Shape 3: { messages: [...] }
    msgs = value.get("messages", [])
    if msgs and not tabs and not conv:
        turns = _conv_to_turns(msgs)
        if turns:
            sessions.append(_make_session(turns, "Cursor chat", db_path))

    return sessions


def _conv_to_turns(conv: list) -> list[Turn]:
    turns = []
    for msg in conv:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", msg.get("type", ""))
        if role in ("human", "user"):
            role = "user"
        elif role in ("ai", "assistant", "bot"):
            role = "assistant"
        else:
            continue

        content = msg.get("content", msg.get("text", ""))
        if isinstance(content, list):
            content = " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        content = truncate_content(str(content).strip())
        if not content:
            continue

        # Cursor often omits timestamps — use epoch 0 as placeholder
        ts_raw = msg.get("timestamp", msg.get("createdAt", 0))
        try:
            ts = datetime.fromtimestamp(float(ts_raw) / 1000, tz=timezone.utc)
        except Exception:
            ts = datetime.now(timezone.utc)

        turns.append(Turn(role=role, content=content, timestamp=ts))

    return sorted(turns, key=lambda t: t.timestamp)


def _make_session(turns: list[Turn], title: str, db_path: Path) -> NormalizedSession:
    # Derive project from the workspace path segment
    workspace_id = db_path.parent.name
    return NormalizedSession(
        session_id=f"cursor:{workspace_id}:{hash(turns[0].content) & 0xFFFFFF:06x}",
        source="cursor",
        project_path="",
        project_name=title,
        date=turns[0].timestamp.date().isoformat(),
        started_at=turns[0].timestamp,
        ended_at=turns[-1].timestamp,
        turns=turns,
        metadata=compute_metadata(turns),
        raw_source_path=str(db_path),
    )


async def ingest(date: str | None = None) -> AsyncIterator[NormalizedSession]:
    if not _WORKSPACE_ROOT.exists():
        return

    for workspace_dir in _WORKSPACE_ROOT.iterdir():
        db_path = workspace_dir / "state.vscdb"
        if not db_path.exists():
            continue

        blobs = _extract_chats_from_db(db_path)
        for blob in blobs:
            for session in _parse_chat_blob(blob, db_path):
                if date and session.date != date:
                    continue
                yield session
