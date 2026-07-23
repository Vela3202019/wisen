"""
Pi (pi.ai) connector.

Pi is a web-only product — there is no local session storage on disk.
This connector reads Pi conversations from exported files dropped into
the generic exports folder, plus supports Pi's own data export format.

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
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from wisen import config
from wisen.ingestion.normalizer import (
    NormalizedSession, Turn, compute_metadata, truncate_content,
)

_DEFAULT_PI_EXPORT_DIR = config.root() / ".wisen" / "exports" / "pi"

_ROLE_MAP = {
    "human": "user",
    "user": "user",
    "assistant": "assistant",
    "pi": "assistant",
    "ai": "assistant",
}


def _parse_ts(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)


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
    ts_base = datetime.now(timezone.utc)
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


async def ingest(
    export_dir: str | None = None,
    date: str | None = None,
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
            except Exception:
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
            except Exception:
                continue

        for session in sessions:
            if date and session.date != date:
                continue
            yield session
