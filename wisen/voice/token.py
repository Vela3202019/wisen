"""Generate LiveKit participant tokens with agent dispatch."""
import os
import uuid
from livekit.api import AccessToken, RoomAgentDispatch, RoomConfiguration, VideoGrants

LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "ws://localhost:7880")
API_KEY = os.environ.get("LIVEKIT_API_KEY", "devkey")
API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "secret")
AGENT_NAME = "wisen-voice"


def create_participant_token() -> tuple[str, str]:
    """Returns (jwt, room_name). Room name is unique per session so dispatch always fires."""
    identity = f"user-{uuid.uuid4().hex[:8]}"
    room_name = f"wisen-{uuid.uuid4().hex[:8]}"
    token = (
        AccessToken(api_key=API_KEY, api_secret=API_SECRET)
        .with_identity(identity)
        .with_name("User")
        .with_grants(VideoGrants(room_join=True, room=room_name, can_publish=True, can_subscribe=True))
        .with_room_config(
            RoomConfiguration(
                agents=[RoomAgentDispatch(agent_name=AGENT_NAME)],
            )
        )
    )
    return token.to_jwt(), room_name
