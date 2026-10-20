from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    timestamp: datetime
    has_tool_call: bool = False
    tool_names: list[str] = []


class SessionMetadata(BaseModel):
    total_turns: int
    user_turns: int
    assistant_turns: int
    session_length_minutes: float


class NormalizedSession(BaseModel):
    session_id: str
    source: Literal["claude-code", "cursor", "opencode", "codex", "gemini-cli", "pi", "export", "git"]
    project_path: str
    project_name: str
    date: str                 # YYYY-MM-DD
    started_at: datetime
    ended_at: datetime
    turns: list[Turn]         # held in memory for synthesis; NOT persisted to the store
    metadata: SessionMetadata
    raw_source_path: str      # local path — synthesis agent re-reads this for content

    def meta_doc(self) -> dict:
        """Storable dict: metadata signals only, no turns content."""
        return {
            "session_id": self.session_id,
            "source": self.source,
            "project_path": self.project_path,
            "project_name": self.project_name,
            "date": self.date,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "raw_source_path": self.raw_source_path,
            "metadata": self.metadata.model_dump(),
        }


def compute_metadata(turns: list[Turn]) -> SessionMetadata:
    """Objective session facts only. Qualitative friction signals (rephrases,
    hedges, clarifications) are the Observer LLM's job — it reads the turns."""
    if not turns:
        return SessionMetadata(
            total_turns=0, user_turns=0, assistant_turns=0,
            session_length_minutes=0.0,
        )

    user_turns = [t for t in turns if t.role == "user"]
    assistant_turns = [t for t in turns if t.role == "assistant"]

    duration = (turns[-1].timestamp - turns[0].timestamp).total_seconds() / 60

    return SessionMetadata(
        total_turns=len(turns),
        user_turns=len(user_turns),
        assistant_turns=len(assistant_turns),
        session_length_minutes=round(duration, 2),
    )


def truncate_content(content: str, max_chars: int = 2000) -> str:
    """Keep content under limit to avoid blowing the synthesis context window."""
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + f"… [truncated {len(content) - max_chars} chars]"
