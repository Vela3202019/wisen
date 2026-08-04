from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from wisen import config

load_dotenv()

_client: AsyncIOMotorClient | None = None


def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        uri = config.get("storage", "mongodb_uri", "MONGODB_URI", "mongodb://127.0.0.1:27017")
        _client = AsyncIOMotorClient(uri)
    return _client


def get_db() -> AsyncIOMotorDatabase:
    db_name = config.get("storage", "mongodb_db", "MONGODB_DB", "wisen")
    return get_client()[db_name]


async def ensure_indexes():
    db = get_db()
    await db.reflections.create_index([("date", 1)], unique=True)
    await db.chat_sessions.create_index([("session_id", 1)], unique=True)
    await db.chat_sessions.create_index([("updated_at", -1)])
    await db.voice_sessions.create_index([("room_name", 1)], unique=True)
    await db.voice_sessions.create_index([("started_at", -1)])
