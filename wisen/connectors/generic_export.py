"""
Watches ~/.wisen/exports/ for manually dropped conversation exports.
Supports:
  - *.json  → { source?, turns: [{role, content, timestamp?}] }
  - *.md    → treated as a single-turn note
  - *.txt   → treated as a single-turn note

Use this for Pi, ChatGPT exports, or any agent without a local connector.
"""
import json
import os
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

_DEFAULT_EXPORT_FOLDER = config.root() / ".wisen" / "exports"


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_json_export(path: Path) -> NormalizedSession | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    source_label = data.get("source", path.stem)
    raw_turns = data.get("turns", data.get("messages", []))
    turns = []
    for t in raw_turns:
        role = t.get("role", "user")
        if role not in ("user", "assistant"):
            continue
        content = truncate_content(str(t.get("content", "")).strip())
        if not content:
            continue
        ts_raw = t.get("timestamp", t.get("created_at", None))
        try:
            ts = datetime.fromisoformat(str(ts_raw)) if ts_raw else _now()
        except ValueError:
            ts = _now()
        turns.append(Turn(role=role, content=content, timestamp=ts))

    if not turns:
        return None

    turns.sort(key=lambda t: t.timestamp)
    date = turns[0].timestamp.date().isoformat()
    return NormalizedSession(
        session_id=f"export:{path.stem}",
        source="export",
        project_path="",
        project_name=source_label,
        date=date,
        started_at=turns[0].timestamp,
        ended_at=turns[-1].timestamp,
        turns=turns,
        metadata=compute_metadata(turns),
        raw_source_path=str(path),
    )


def _parse_text_export(path: Path) -> NormalizedSession | None:
    content = path.read_text(encoding="utf-8", errors="replace").strip()
    if not content:
        return None
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    turns = [Turn(role="user", content=truncate_content(content), timestamp=mtime)]
    return NormalizedSession(
        session_id=f"export:{path.stem}",
        source="export",
        project_path="",
        project_name=path.stem,
        date=mtime.date().isoformat(),
        started_at=mtime,
        ended_at=mtime,
        turns=turns,
        metadata=compute_metadata(turns),
        raw_source_path=str(path),
    )


async def ingest(
    export_folder: str | None = None,
    date: str | None = None,
) -> AsyncIterator[NormalizedSession]:
    root = Path(export_folder) if export_folder else Path(
        os.getenv("EXPORT_FOLDER", str(_DEFAULT_EXPORT_FOLDER))
    )
    if not root.exists():
        return

    for f in root.iterdir():
        if f.suffix == ".json":
            session = _parse_json_export(f)
        elif f.suffix in (".md", ".txt"):
            session = _parse_text_export(f)
        else:
            continue

        if session is None:
            continue
        if date and session.date != date:
            continue
        yield session
