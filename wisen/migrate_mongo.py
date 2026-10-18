"""
One-off copy of a pre-SQLite wisen MongoDB (reflections, chat_sessions,
voice_sessions) into the SQLite store. Opt-in via `wisen migrate-mongo`;
idempotent — re-running replaces each copied record rather than duplicating it.
Needs pymongo: pip install 'wisen[migrate]'.
"""
import asyncio
from datetime import datetime

from wisen import config
from wisen.store import SqliteStore, get_store, iso


def _ts(value) -> str | None:
    return iso(value) if isinstance(value, datetime) else value


def _messages(doc: dict) -> list[dict]:
    return [
        {"role": m.get("role", ""), "content": m.get("content", ""), "at": _ts(m.get("at"))}
        for m in doc.get("messages", [])
    ]


async def copy_all(mongo_db, store: SqliteStore) -> dict[str, int]:
    counts = {"reflections": 0, "chat_sessions": 0, "voice_sessions": 0}

    for doc in mongo_db.reflections.find({}):
        doc.pop("_id", None)
        if not doc.get("date"):
            continue
        await store.upsert_reflection(doc)
        counts["reflections"] += 1

    for doc in mongo_db.chat_sessions.find({}):
        await store.put_chat_session({
            "session_id": doc["session_id"],
            "kind": doc.get("kind", "text"),
            "created_at": _ts(doc.get("created_at")),
            "updated_at": _ts(doc.get("updated_at") or doc.get("created_at")),
            "messages": _messages(doc),
        })
        counts["chat_sessions"] += 1

    for doc in mongo_db.voice_sessions.find({}):
        await store.put_voice_session({
            "room_name": doc["room_name"],
            "started_at": _ts(doc.get("started_at")),
            "updated_at": _ts(doc.get("updated_at")),
            "messages": _messages(doc),
        })
        counts["voice_sessions"] += 1

    return counts


def main() -> None:
    uri = config.get("storage", "mongodb_uri", "MONGODB_URI", "mongodb://127.0.0.1:27017")
    db_name = config.get("storage", "mongodb_db", "MONGODB_DB", "wisen")
    try:
        from pymongo import MongoClient
        from pymongo.errors import PyMongoError
    except ImportError:
        raise SystemExit("migrate-mongo needs pymongo: pip install 'wisen[migrate]'")

    client = MongoClient(uri, serverSelectionTimeoutMS=3000, tz_aware=True)
    try:
        client.admin.command("ping")
    except PyMongoError:
        print(f"no MongoDB reachable at {uri} — nothing to migrate")
        return

    store = get_store()
    with client:
        counts = asyncio.run(copy_all(client[db_name], store))
    summary = ", ".join(f"{n} {name}" for name, n in counts.items())
    print(f"copied {summary} from {uri}/{db_name} into {store.path}")
