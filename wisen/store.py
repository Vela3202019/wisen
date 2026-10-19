"""
Local storage: what wisen persists (reflections, chat and voice conversations),
behind a small `Store` interface with one SQLite implementation.

One SQLite file (WAL mode) at `[storage].sqlite_path` / WISEN_DB_PATH, default
~/.wisen/wisen.db. Every row carries a user id (`[storage].user_id` /
WISEN_USER_ID, default "local") so nothing assumes a single user. Raw
transcripts are never stored here.

Timestamps are stored and returned as ISO-8601 UTC strings.
"""
import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from dotenv import load_dotenv

from wisen import config

load_dotenv()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reflections (
    user_id TEXT NOT NULL,
    date    TEXT NOT NULL,
    doc     TEXT NOT NULL,
    PRIMARY KEY (user_id, date)
);
CREATE TABLE IF NOT EXISTS chat_sessions (
    user_id    TEXT NOT NULL,
    session_id TEXT NOT NULL,
    kind       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, session_id)
);
CREATE INDEX IF NOT EXISTS chat_sessions_updated ON chat_sessions (user_id, updated_at);
CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY,
    user_id    TEXT NOT NULL,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_messages_session ON chat_messages (user_id, session_id, at, id);
CREATE TABLE IF NOT EXISTS voice_sessions (
    user_id    TEXT NOT NULL,
    room_name  TEXT NOT NULL,
    started_at TEXT NOT NULL,
    updated_at TEXT,
    PRIMARY KEY (user_id, room_name)
);
CREATE TABLE IF NOT EXISTS voice_messages (
    id        INTEGER PRIMARY KEY,
    user_id   TEXT NOT NULL,
    room_name TEXT NOT NULL,
    role      TEXT NOT NULL,
    content   TEXT NOT NULL,
    at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS voice_messages_room ON voice_messages (user_id, room_name, at, id);
"""


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _json_default(value):
    if isinstance(value, datetime):
        return iso(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


class Store(Protocol):
    async def ping(self) -> bool: ...

    async def get_reflection(self, date: str) -> dict | None: ...
    async def list_reflections(self, limit: int | None = None, before: str | None = None) -> list[dict]:
        """Newest first; `before` keeps only dates strictly earlier than it."""
    async def upsert_reflection(self, doc: dict) -> None:
        """Insert or replace the reflection for doc["date"]."""

    async def chat_session_exists(self, session_id: str) -> bool: ...
    async def create_chat_session(self, session_id: str, kind: str, at: datetime) -> None: ...
    async def append_chat_message(self, session_id: str, role: str, content: str, at: datetime) -> None: ...
    async def list_chat_sessions(self, limit: int) -> list[dict]:
        """Most recently updated first, each with its `first_message` (or None)."""
    async def get_chat_session(self, session_id: str) -> dict | None:
        """The session with its `messages` in order."""

    async def start_voice_session(self, room_name: str, at: datetime) -> None:
        """Create the voice session unless it already exists."""
    async def append_voice_message(self, room_name: str, role: str, content: str, at: datetime) -> None: ...


class SqliteStore:
    def __init__(self, path: Path, user_id: str):
        self.path = path
        self.user_id = user_id
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        # One short-lived connection per operation, opened on the worker thread
        # that uses it; WAL lets the API server and voice worker share the file.
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    async def _run(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    # ── health ────────────────────────────────────────────────────────────────

    def _ping(self) -> bool:
        with closing(self._connect()) as conn:
            return conn.execute("SELECT 1").fetchone()[0] == 1

    async def ping(self) -> bool:
        return await self._run(self._ping)

    # ── reflections ───────────────────────────────────────────────────────────

    def _get_reflection(self, date: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT doc FROM reflections WHERE user_id = ? AND date = ?", (self.user_id, date)
            ).fetchone()
        return json.loads(row["doc"]) if row else None

    async def get_reflection(self, date: str) -> dict | None:
        return await self._run(self._get_reflection, date)

    def _list_reflections(self, limit: int | None, before: str | None) -> list[dict]:
        sql = "SELECT doc FROM reflections WHERE user_id = ?"
        params: list = [self.user_id]
        if before is not None:
            sql += " AND date < ?"
            params.append(before)
        sql += " ORDER BY date DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with closing(self._connect()) as conn:
            return [json.loads(r["doc"]) for r in conn.execute(sql, params)]

    async def list_reflections(self, limit: int | None = None, before: str | None = None) -> list[dict]:
        return await self._run(self._list_reflections, limit, before)

    def _upsert_reflection(self, doc: dict) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO reflections (user_id, date, doc) VALUES (?, ?, ?) "
                "ON CONFLICT (user_id, date) DO UPDATE SET doc = excluded.doc",
                (self.user_id, doc["date"], json.dumps(doc, default=_json_default)),
            )

    async def upsert_reflection(self, doc: dict) -> None:
        await self._run(self._upsert_reflection, doc)

    # ── chat sessions ─────────────────────────────────────────────────────────

    def _chat_session_exists(self, session_id: str) -> bool:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT 1 FROM chat_sessions WHERE user_id = ? AND session_id = ?",
                (self.user_id, session_id),
            ).fetchone() is not None

    async def chat_session_exists(self, session_id: str) -> bool:
        return await self._run(self._chat_session_exists, session_id)

    def _create_chat_session(self, session_id: str, kind: str, at: datetime) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO chat_sessions (user_id, session_id, kind, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (self.user_id, session_id, kind, iso(at), iso(at)),
            )

    async def create_chat_session(self, session_id: str, kind: str, at: datetime) -> None:
        await self._run(self._create_chat_session, session_id, kind, at)

    def _append_chat_message(self, session_id: str, role: str, content: str, at: datetime) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO chat_messages (user_id, session_id, role, content, at) VALUES (?, ?, ?, ?, ?)",
                (self.user_id, session_id, role, content, iso(at)),
            )
            conn.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE user_id = ? AND session_id = ?",
                (iso(at), self.user_id, session_id),
            )

    async def append_chat_message(self, session_id: str, role: str, content: str, at: datetime) -> None:
        await self._run(self._append_chat_message, session_id, role, content, at)

    def _list_chat_sessions(self, limit: int) -> list[dict]:
        with closing(self._connect()) as conn:
            sessions = [dict(r) for r in conn.execute(
                "SELECT session_id, kind, created_at, updated_at FROM chat_sessions "
                "WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
                (self.user_id, limit),
            )]
            for s in sessions:
                first = conn.execute(
                    "SELECT role, content, at FROM chat_messages WHERE user_id = ? AND session_id = ? "
                    "ORDER BY at, id LIMIT 1",
                    (self.user_id, s["session_id"]),
                ).fetchone()
                s["first_message"] = dict(first) if first else None
        return sessions

    async def list_chat_sessions(self, limit: int) -> list[dict]:
        return await self._run(self._list_chat_sessions, limit)

    def _get_chat_session(self, session_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT session_id, kind, created_at, updated_at FROM chat_sessions "
                "WHERE user_id = ? AND session_id = ?",
                (self.user_id, session_id),
            ).fetchone()
            if not row:
                return None
            messages = conn.execute(
                "SELECT role, content, at FROM chat_messages WHERE user_id = ? AND session_id = ? "
                "ORDER BY at, id",
                (self.user_id, session_id),
            )
            return {**dict(row), "messages": [dict(m) for m in messages]}

    async def get_chat_session(self, session_id: str) -> dict | None:
        return await self._run(self._get_chat_session, session_id)

    # ── voice sessions ────────────────────────────────────────────────────────

    def _start_voice_session(self, room_name: str, at: datetime) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO voice_sessions (user_id, room_name, started_at) VALUES (?, ?, ?) "
                "ON CONFLICT (user_id, room_name) DO NOTHING",
                (self.user_id, room_name, iso(at)),
            )

    async def start_voice_session(self, room_name: str, at: datetime) -> None:
        await self._run(self._start_voice_session, room_name, at)

    def _append_voice_message(self, room_name: str, role: str, content: str, at: datetime) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO voice_messages (user_id, room_name, role, content, at) VALUES (?, ?, ?, ?, ?)",
                (self.user_id, room_name, role, content, iso(at)),
            )
            conn.execute(
                "UPDATE voice_sessions SET updated_at = ? WHERE user_id = ? AND room_name = ?",
                (iso(at), self.user_id, room_name),
            )

    async def append_voice_message(self, room_name: str, role: str, content: str, at: datetime) -> None:
        await self._run(self._append_voice_message, room_name, role, content, at)

    # ── import (used by `wisen migrate-mongo`) ────────────────────────────────

    async def import_reflection(self, doc: dict) -> bool:
        """Insert a reflection unless its date already exists; True if copied."""
        return await self._run(self._import_reflection, doc)

    def _import_reflection(self, doc: dict) -> bool:
        with closing(self._connect()) as conn, conn:
            return conn.execute(
                "INSERT INTO reflections (user_id, date, doc) VALUES (?, ?, ?) "
                "ON CONFLICT (user_id, date) DO NOTHING",
                (self.user_id, doc["date"], json.dumps(doc, default=_json_default)),
            ).rowcount == 1

    async def import_chat_session(self, session: dict) -> bool:
        """Insert a chat session and its messages unless it already exists; True if copied."""
        return await self._run(
            self._import_conversation, session, "chat_sessions", "chat_messages", "session_id",
            ("kind", "created_at", "updated_at"),
        )

    async def import_voice_session(self, session: dict) -> bool:
        """Insert a voice session and its messages unless it already exists; True if copied."""
        return await self._run(
            self._import_conversation, session, "voice_sessions", "voice_messages", "room_name",
            ("started_at", "updated_at"),
        )

    def _import_conversation(self, session: dict, table: str, msg_table: str, key: str, cols: tuple) -> bool:
        value = session[key]
        with closing(self._connect()) as conn, conn:
            inserted = conn.execute(
                f"INSERT INTO {table} (user_id, {key}, {', '.join(cols)}) "
                f"VALUES (?, ?, {', '.join('?' * len(cols))}) ON CONFLICT (user_id, {key}) DO NOTHING",
                (self.user_id, value, *(session.get(c) for c in cols)),
            ).rowcount == 1
            if inserted:
                conn.executemany(
                    f"INSERT INTO {msg_table} (user_id, {key}, role, content, at) VALUES (?, ?, ?, ?, ?)",
                    [(self.user_id, value, m["role"], m["content"], m["at"]) for m in session.get("messages", [])],
                )
            return inserted


_store: SqliteStore | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        path = Path(config.get("storage", "sqlite_path", "WISEN_DB_PATH", "~/.wisen/wisen.db")).expanduser()
        user_id = config.get("storage", "user_id", "WISEN_USER_ID", "local")
        _store = SqliteStore(path, user_id)
    return _store
