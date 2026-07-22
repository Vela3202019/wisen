"""
Reads opencode sessions from ~/.local/share/opencode/opencode.db (SQLite).

Schema (v1.17+):
  session  — id, title, directory, time_created, time_updated
  message  — id, session_id, time_created, data (JSON: {role, ...})
  part     — id, message_id, session_id, time_created, data (JSON: {type, text, ...})
"""
import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from ingestion.normalizer import (
    NormalizedSession, Turn, compute_metadata, truncate_content
)

_DB_PATH = Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def _ts(ms: int | None) -> datetime:
    if ms is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _read_sessions(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("SELECT id, title, directory, time_created, time_updated FROM session")
        sessions = [dict(r) for r in cur.fetchall()]

        # data column contains JSON with role
        cur.execute("SELECT id, session_id, time_created, data FROM message ORDER BY time_created")
        messages = [dict(r) for r in cur.fetchall()]

        # data column contains JSON with type + text
        cur.execute("SELECT message_id, data FROM part ORDER BY time_created")
        parts = [dict(r) for r in cur.fetchall()]

        conn.close()
        return _assemble(sessions, messages, parts)
    except Exception:
        return []


def _parse_data(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _assemble(sessions, messages, parts) -> list[dict]:
    # Collect text content per message_id
    parts_by_msg: dict[str, list[str]] = {}
    for p in parts:
        mid = p["message_id"]
        d = _parse_data(p.get("data"))
        if d.get("type") == "text" and d.get("text"):
            parts_by_msg.setdefault(mid, []).append(d["text"])

    # Parse role from message.data, attach text content
    msgs_by_session: dict[str, list[dict]] = {}
    for m in messages:
        d = _parse_data(m.get("data"))
        m["role"] = d.get("role", "")
        m["content"] = " ".join(parts_by_msg.get(m["id"], []))
        msgs_by_session.setdefault(m["session_id"], []).append(m)

    return [
        {"session": s, "messages": msgs_by_session.get(s["id"], [])}
        for s in sessions
    ]


async def ingest(date: str | None = None) -> AsyncIterator[NormalizedSession]:
    for raw in _read_sessions(_DB_PATH):
        s = raw["session"]
        msgs = raw["messages"]
        if not msgs:
            continue

        turns = []
        for m in msgs:
            role = m.get("role", "")
            if role not in ("user", "assistant"):
                continue
            content = truncate_content(m.get("content", "").strip())
            if not content:
                continue
            turns.append(Turn(
                role=role,
                content=content,
                timestamp=_ts(m.get("time_created")),
            ))

        if not turns:
            continue

        turns.sort(key=lambda t: t.timestamp)
        session_date = turns[0].timestamp.date().isoformat()
        if date and session_date != date:
            continue

        yield NormalizedSession(
            session_id=f"opencode:{s['id']}",
            source="opencode",
            project_path=s.get("directory", ""),
            project_name=s.get("title") or Path(s.get("directory", "opencode")).name,
            date=session_date,
            started_at=turns[0].timestamp,
            ended_at=turns[-1].timestamp,
            turns=turns,
            metadata=compute_metadata(turns),
            raw_source_path=str(_DB_PATH),
        )
