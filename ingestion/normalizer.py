from pydantic import BaseModel
from datetime import datetime
from typing import Literal
import re


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
    user_rephrases: int       # user sent near-duplicate prompt within 3 turns
    agent_clarifications: int # assistant asked user to clarify
    agent_hedges: int         # "I think", "I'm not sure", "you might want to"
    agent_contradictions: int # assistant reversed its own answer


class NormalizedSession(BaseModel):
    session_id: str
    source: Literal["claude-code", "cursor", "opencode", "codex", "gemini-cli", "pi", "export", "git"]
    project_path: str
    project_name: str
    date: str                 # YYYY-MM-DD
    started_at: datetime
    ended_at: datetime
    turns: list[Turn]         # held in memory for synthesis; NOT persisted to MongoDB
    metadata: SessionMetadata
    raw_source_path: str      # local path — synthesis agent re-reads this for content

    def meta_doc(self) -> dict:
        """MongoDB-safe dict: metadata signals only, no turns content."""
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


_CLARIFICATION_PATTERNS = re.compile(
    r"\b(could you clarify|what do you mean|can you be more specific"
    r"|could you elaborate|what exactly|which one do you mean)\b",
    re.IGNORECASE,
)
_HEDGE_PATTERNS = re.compile(
    r"\b(I think|I believe|I'm not sure|you might want to|it's possible"
    r"|I'm unsure|I'm not certain|this may|this might)\b",
    re.IGNORECASE,
)


def compute_metadata(turns: list[Turn]) -> SessionMetadata:
    if not turns:
        return SessionMetadata(
            total_turns=0, user_turns=0, assistant_turns=0,
            session_length_minutes=0.0,
            user_rephrases=0, agent_clarifications=0,
            agent_hedges=0, agent_contradictions=0,
        )

    user_turns = [t for t in turns if t.role == "user"]
    assistant_turns = [t for t in turns if t.role == "assistant"]

    duration = (turns[-1].timestamp - turns[0].timestamp).total_seconds() / 60

    # User rephrases: cosine similarity would be ideal but too slow here.
    # Proxy: user sent messages within 60s of each other (rapid correction).
    rephrases = 0
    for i in range(1, len(user_turns)):
        gap = (user_turns[i].timestamp - user_turns[i - 1].timestamp).total_seconds()
        if gap < 60:
            rephrases += 1

    clarifications = sum(
        1 for t in assistant_turns
        if _CLARIFICATION_PATTERNS.search(t.content)
    )
    hedges = sum(
        1 for t in assistant_turns
        if _HEDGE_PATTERNS.search(t.content)
    )

    return SessionMetadata(
        total_turns=len(turns),
        user_turns=len(user_turns),
        assistant_turns=len(assistant_turns),
        session_length_minutes=round(duration, 2),
        user_rephrases=rephrases,
        agent_clarifications=clarifications,
        agent_hedges=hedges,
        agent_contradictions=0,  # computed by synthesis agent from content
    )


def truncate_content(content: str, max_chars: int = 2000) -> str:
    """Keep content under limit to avoid blowing the synthesis context window."""
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + f"… [truncated {len(content) - max_chars} chars]"
