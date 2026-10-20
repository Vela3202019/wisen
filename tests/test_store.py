"""
SQLite store on a temp file: reflection round-trip and upsert-by-date, chat
history order, per-user isolation, and the insert-only MongoDB import. No
MongoDB needed. Run: python3 -m tests.test_store
"""
import asyncio
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from wisen.migrate_mongo import copy_all
from wisen.store import SqliteStore, iso

T0 = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)


def _reflection(date: str, score: float) -> dict:
    return {
        "date": date,
        "alignment_score": score,
        "alignment_label": "aligned",
        "your_mood": {"label": "deep-focus", "summary": "s", "evidence": "a quote"},
        "tldr": ["one", "two"],
    }


async def check_reflections(store: SqliteStore):
    doc = {**_reflection("2026-10-16", 0.7), "synthesized_at": T0}
    await store.upsert_reflection(doc)
    got = await store.get_reflection("2026-10-16")
    assert got == {**_reflection("2026-10-16", 0.7), "synthesized_at": T0.isoformat()}, got
    assert await store.get_reflection("2026-10-15") is None

    # Upsert by date replaces, never duplicates
    await store.upsert_reflection(_reflection("2026-10-16", 0.9))
    assert (await store.get_reflection("2026-10-16"))["alignment_score"] == 0.9
    assert len(await store.list_reflections()) == 1

    for date in ("2026-10-14", "2026-10-17", "2026-10-15"):
        await store.upsert_reflection(_reflection(date, 0.5))
    dates = [d["date"] for d in await store.list_reflections()]
    assert dates == ["2026-10-17", "2026-10-16", "2026-10-15", "2026-10-14"], dates
    dates = [d["date"] for d in await store.list_reflections(limit=2, before="2026-10-17")]
    assert dates == ["2026-10-16", "2026-10-15"], dates
    print("✓ store: reflection round-trip, upsert-by-date, newest-first listing")


async def check_chat(store: SqliteStore):
    await store.create_chat_session("s1", "text", T0)
    await store.create_chat_session("s2", "text", T0)
    turns = [("user", "how was my week?"), ("assistant", "busy"), ("user", "why?"), ("assistant", "hooks")]
    for i, (role, content) in enumerate(turns):
        await store.append_chat_message("s1", role, content, T0 + timedelta(seconds=i))
    # Same timestamp: insertion order breaks the tie
    await store.append_chat_message("s2", "user", "first", T0 + timedelta(minutes=5))
    await store.append_chat_message("s2", "assistant", "second", T0 + timedelta(minutes=5))

    s1 = await store.get_chat_session("s1")
    assert [(m["role"], m["content"]) for m in s1["messages"]] == turns, s1
    assert s1["created_at"] == T0.isoformat()
    assert s1["updated_at"] == (T0 + timedelta(seconds=3)).isoformat()
    s2 = await store.get_chat_session("s2")
    assert [m["content"] for m in s2["messages"]] == ["first", "second"], s2
    assert await store.get_chat_session("missing") is None
    assert await store.chat_session_exists("s1") and not await store.chat_session_exists("missing")

    listed = await store.list_chat_sessions(10)
    assert [s["session_id"] for s in listed] == ["s2", "s1"], listed
    assert listed[1]["first_message"]["content"] == "how was my week?"
    assert [s["session_id"] for s in await store.list_chat_sessions(1)] == ["s2"]
    print("✓ store: chat history in order, sessions newest-updated first")


async def check_voice(store: SqliteStore):
    await store.start_voice_session("room-1", T0)
    await store.start_voice_session("room-1", T0 + timedelta(hours=1))  # no-op: already started
    await store.append_voice_message("room-1", "user", "hi", T0 + timedelta(seconds=1))
    with sqlite3.connect(store.path) as conn:
        started = conn.execute("SELECT started_at FROM voice_sessions").fetchall()
        msgs = conn.execute("SELECT content FROM voice_messages").fetchall()
    assert started == [(T0.isoformat(),)], started
    assert msgs == [("hi",)], msgs
    print("✓ store: voice session started once, messages appended")


async def check_user_isolation(path: Path):
    alice, bob = SqliteStore(path, "alice"), SqliteStore(path, "bob")
    await alice.upsert_reflection(_reflection("2026-10-16", 0.1))
    await alice.create_chat_session("shared-id", "text", T0)
    assert await bob.get_reflection("2026-10-16") is None
    assert await bob.list_reflections() == []
    assert not await bob.chat_session_exists("shared-id")
    await bob.create_chat_session("shared-id", "text", T0)  # same id, other user: no clash
    assert await bob.list_chat_sessions(10) != [] and len(await alice.list_chat_sessions(10)) == 1
    print("✓ store: rows are scoped to their user id")


class _FakeCollection:
    def __init__(self, docs):
        self._docs = docs

    def find(self, _query):
        return [dict(d) for d in self._docs]  # fresh copies, like a cursor


class _FakeMongo:
    def __init__(self):
        at = datetime(2026, 10, 1, 10, 0, tzinfo=timezone(timedelta(hours=2)))
        msgs = [{"role": "user", "content": "q", "at": at},
                {"role": "assistant", "content": "a", "at": at + timedelta(seconds=2)}]
        self.reflections = _FakeCollection([
            {"_id": "oid1", **_reflection("2026-10-01", 0.4), "synthesized_at": at},
            {"_id": "oid2", "alignment_score": 0.1},  # no date: skipped
        ])
        self.chat_sessions = _FakeCollection([
            {"_id": "oid3", "session_id": "c1", "kind": "text", "created_at": at,
             "updated_at": at + timedelta(seconds=2), "messages": msgs},
        ])
        self.voice_sessions = _FakeCollection([
            {"_id": "oid4", "room_name": "r1", "started_at": at, "messages": msgs[:1]},
        ])


async def check_migration(path: Path):
    store = SqliteStore(path, "local")
    counts = await copy_all(_FakeMongo(), store)
    assert counts == {"reflections": 1, "chat_sessions": 1, "voice_sessions": 1}, counts
    refl = await store.list_reflections()
    assert len(refl) == 1 and "_id" not in refl[0], refl
    assert refl[0]["synthesized_at"] == "2026-10-01T08:00:00+00:00", refl
    c1 = await store.get_chat_session("c1")
    assert [m["content"] for m in c1["messages"]] == ["q", "a"], c1
    assert c1["messages"][0]["at"] == "2026-10-01T08:00:00+00:00", c1
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM voice_messages").fetchone()[0] == 1

    # Data written to SQLite after the first migration must survive a re-run.
    later = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
    await store.append_chat_message("c1", "user", "follow-up", later)
    await store.append_voice_message("r1", "user", "spoken", later)
    await store.upsert_reflection(_reflection("2026-10-01", 0.9))

    counts = await copy_all(_FakeMongo(), store)
    assert counts == {"reflections": 0, "chat_sessions": 0, "voice_sessions": 0}, counts
    c1 = await store.get_chat_session("c1")
    assert [m["content"] for m in c1["messages"]] == ["q", "a", "follow-up"], c1
    assert c1["updated_at"] == iso(later), c1
    assert (await store.get_reflection("2026-10-01"))["alignment_score"] == 0.9
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM voice_messages").fetchone()[0] == 2
    print("✓ store: MongoDB import is insert-only, normalizes timestamps, keeps local data")


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        store = SqliteStore(Path(tmp) / "nested" / "wisen.db", "local")
        with sqlite3.connect(store.path) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert await store.ping()
        await check_reflections(store)
        await check_chat(store)
        await check_voice(store)
        await check_user_isolation(Path(tmp) / "users.db")
        await check_migration(Path(tmp) / "migrated.db")
    print("\nall store tests passed")


if __name__ == "__main__":
    asyncio.run(main())
