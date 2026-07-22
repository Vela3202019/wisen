"""
Reads Codex (OpenAI) sessions from ~/.codex/.
Session files live in:
  ~/.codex/sessions/           — active sessions (JSONL)
  ~/.codex/archived_sessions/  — completed sessions (JSONL)

Each JSONL line: { timestamp, type, payload }
Relevant types:
  session_meta   — session id, cwd, model
  response_item  — { type:"message", role:"user"|"assistant"|"developer", content:[{type,text}] }
  event_msg      — user_message events with plain text
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from ingestion.normalizer import (
    NormalizedSession, Turn, compute_metadata, truncate_content,
)

_CODEX_DIR = Path.home() / ".codex"
_SESSION_DIRS = ["sessions", "archived_sessions"]

# Roles to skip (system/developer instructions injected by Codex itself)
_SKIP_ROLES = {"developer", "system"}

# event_msg types that carry the actual user text
_USER_MSG_TYPES = {"user_message"}


def _parse_session_file(path: Path) -> list[NormalizedSession]:
    events: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        return []

    if not events:
        return []

    # Extract session metadata
    meta = next(
        (e["payload"] for e in events if e.get("type") == "session_meta"),
        {},
    )
    session_id = meta.get("id", path.stem)
    cwd = meta.get("cwd", "")
    project_name = Path(cwd).name if cwd else path.stem

    turns: list[Turn] = []

    for e in events:
        etype = e.get("type")
        payload = e.get("payload", {})
        ts_str = e.get("timestamp", "")

        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            ts = datetime.now(timezone.utc)

        # response_item: assistant or user message
        if etype == "response_item":
            role = payload.get("role", "")
            if role in _SKIP_ROLES:
                continue
            if role not in ("user", "assistant"):
                continue

            content_blocks = payload.get("content", [])
            text = " ".join(
                b.get("text", "")
                for b in content_blocks
                if isinstance(b, dict) and b.get("type") in ("input_text", "output_text", "text")
            ).strip()

            # Skip Codex-injected system context blocks
            if text.startswith("<environment_context>") or text.startswith("<permissions"):
                continue
            if not text:
                continue

            turns.append(Turn(
                role=role,
                content=truncate_content(text),
                timestamp=ts,
            ))

        # event_msg with user_message type — captures the raw user prompt text
        elif etype == "event_msg":
            msg_type = payload.get("type", "")
            if msg_type not in _USER_MSG_TYPES:
                continue
            text = payload.get("message", "").strip()
            if not text:
                # Fall back to text_elements
                elements = payload.get("text_elements", [])
                text = " ".join(
                    el.get("text", "") for el in elements if isinstance(el, dict)
                ).strip()
            if not text:
                continue
            # Deduplicate: skip if same content as last user turn (Codex sometimes emits both)
            if turns and turns[-1].role == "user" and turns[-1].content[:50] == text[:50]:
                continue
            turns.append(Turn(
                role="user",
                content=truncate_content(text),
                timestamp=ts,
            ))

    if not turns:
        return []

    turns.sort(key=lambda t: t.timestamp)
    date = turns[0].timestamp.date().isoformat()

    return [NormalizedSession(
        session_id=f"codex:{cwd}:{session_id}",
        source="codex",
        project_path=cwd,
        project_name=project_name,
        date=date,
        started_at=turns[0].timestamp,
        ended_at=turns[-1].timestamp,
        turns=turns,
        metadata=compute_metadata(turns),
        raw_source_path=str(path),
    )]


async def ingest(date: str | None = None) -> AsyncIterator[NormalizedSession]:
    if not _CODEX_DIR.exists():
        return

    for subdir_name in _SESSION_DIRS:
        subdir = _CODEX_DIR / subdir_name
        if not subdir.exists():
            continue
        for jsonl_file in subdir.glob("*.jsonl"):
            for session in _parse_session_file(jsonl_file):
                if date and session.date != date:
                    continue
                yield session
