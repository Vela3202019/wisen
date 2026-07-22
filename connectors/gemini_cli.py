"""
Reads Gemini CLI sessions from ~/.gemini/.
The Google Gemini CLI (gemini-cli, @google/gemini-cli) stores conversation
history under ~/.gemini/ when installed.

Known storage layout (as of gemini-cli 0.1.x):
  ~/.gemini/tmp/                      — temp/checkpoint files per session
  ~/.gemini/tmp/gemini-[uuid].json    — session checkpoint (JSON)

Each checkpoint JSON shape:
  {
    "sessionId": "...",
    "conversationHistory": [
      { "role": "user",      "parts": [{ "text": "..." }] },
      { "role": "model",     "parts": [{ "text": "..." }] },
    ],
    "createdAt": "<iso>",
    "updatedAt": "<iso>"
  }

If the layout changes between CLI versions, this connector gracefully
yields nothing rather than crashing.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from ingestion.normalizer import (
    NormalizedSession, Turn, compute_metadata, truncate_content,
)

_GEMINI_DIR = Path.home() / ".gemini"

# Role mapping: Gemini CLI uses "model" for assistant
_ROLE_MAP = {
    "user": "user",
    "model": "assistant",
    "assistant": "assistant",
}


def _parse_ts(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)


def _turns_from_history(history: list[dict], base_ts: datetime) -> list[Turn]:
    turns = []
    # Gemini CLI doesn't always include per-message timestamps —
    # fall back to spacing them 1 second apart from the session start.
    for i, msg in enumerate(history):
        role_raw = msg.get("role", "")
        role = _ROLE_MAP.get(role_raw)
        if role is None:
            continue

        parts = msg.get("parts", [])
        text = " ".join(
            p.get("text", "") for p in parts if isinstance(p, dict)
        ).strip()
        if not text:
            continue

        # Per-message timestamp if present, otherwise synthesize
        ts_raw = msg.get("timestamp") or msg.get("createdAt")
        if ts_raw:
            ts = _parse_ts(ts_raw)
        else:
            from datetime import timedelta
            ts = base_ts + timedelta(seconds=i)

        turns.append(Turn(
            role=role,
            content=truncate_content(text),
            timestamp=ts,
        ))
    return turns


def _parse_checkpoint(path: Path) -> NormalizedSession | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None

    session_id = data.get("sessionId", path.stem)
    history = data.get("conversationHistory", data.get("messages", []))
    if not history:
        return None

    created_at = _parse_ts(data.get("createdAt"))
    turns = _turns_from_history(history, base_ts=created_at)
    if not turns:
        return None

    turns.sort(key=lambda t: t.timestamp)
    date = turns[0].timestamp.date().isoformat()

    return NormalizedSession(
        session_id=f"gemini-cli:{session_id}",
        source="gemini-cli",
        project_path="",
        project_name=data.get("title", "Gemini CLI session"),
        date=date,
        started_at=turns[0].timestamp,
        ended_at=turns[-1].timestamp,
        turns=turns,
        metadata=compute_metadata(turns),
        raw_source_path=str(path),
    )


async def ingest(date: str | None = None) -> AsyncIterator[NormalizedSession]:
    if not _GEMINI_DIR.exists():
        return

    # Search known sub-paths + root for JSON/JSONL session files
    search_paths = [
        _GEMINI_DIR / "tmp",
        _GEMINI_DIR / "sessions",
        _GEMINI_DIR,
    ]

    seen: set[str] = set()
    for search_path in search_paths:
        if not search_path.exists():
            continue
        for f in search_path.glob("**/*.json"):
            if str(f) in seen:
                continue
            seen.add(str(f))
            session = _parse_checkpoint(f)
            if session is None:
                continue
            if date and session.date != date:
                continue
            yield session

        # Also handle JSONL format (future CLI versions may use it)
        for f in search_path.glob("**/*.jsonl"):
            if str(f) in seen:
                continue
            seen.add(str(f))
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            if "conversationHistory" in data or "messages" in data:
                                session = _parse_checkpoint.__wrapped__(f) if hasattr(_parse_checkpoint, "__wrapped__") else None
                        except Exception:
                            pass
            except Exception:
                pass
