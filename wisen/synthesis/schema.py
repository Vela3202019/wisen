from typing import Literal

from pydantic import BaseModel, Field

# ── Sub-models ────────────────────────────────────────────────────────────────

class MindMapNode(BaseModel):
    topic: str
    weight: float = Field(ge=0.0, le=1.0)
    is_new: bool


class MoodSignal(BaseModel):
    label: Literal[
        "deep-focus", "exploratory", "frustrated", "iterating",
        "uncertain", "confident", "stuck", "flowing"
    ]
    summary: str
    evidence: str


class ImplicitQuestion(BaseModel):
    question: str
    evidence: str


class FrictionPoint(BaseModel):
    type: Literal[
        "config-conflict", "missing-skill", "wrong-domain",
        "unclear-prompt", "agent-uncertainty"
    ]
    description: str
    evidence: str


class Recommendation(BaseModel):
    target: Literal["claude-md", "agents-md", "mcp-config", "prompt-style", "workflow"]
    action: str
    reason: str


class DayPattern(BaseModel):
    date: str
    alignment_score: float = Field(ge=0.0, le=1.0)
    alignment_label: Literal["aligned", "mild-friction", "friction", "misaligned"]


# ── Delta models (what changed vs yesterday) ──────────────────────────────────

class ConfigDiff(BaseModel):
    file: str                                                  # e.g. "~/.claude/CLAUDE.md"
    change_type: Literal["added", "removed", "modified"]
    summary: str                                               # plain-English description of the change


class FrictionDelta(BaseModel):
    description: str
    status: Literal["resolved", "persisting", "new"]
    days_persisting: int = 0                                   # 0 = new or resolved today


class ModelUsage(BaseModel):
    source: str                                                # "claude-code", "cursor", "opencode"
    model: str                                                 # "claude-sonnet-4-6", "gpt-4o"
    session_count: int


# ── Stage 1 output: DayObservation ───────────────────────────────────────────
# Free-form per field so the Observer can reason without schema pressure.
# This feeds Stage 2 (Synthesizer) as structured context.

class KeyMoment(BaseModel):
    session_source: str
    quote: str
    significance: str


class DayObservation(BaseModel):
    # Mood signals observed in raw sessions
    mood_signals_user: str       # pattern analysis of how the user wrote prompts
    mood_signals_agent: str      # pattern analysis of how agents responded

    # Topic map
    topics_raw: list[str]        # raw topic list, not yet weighted
    key_moments: list[KeyMoment] # specific session quotes as evidence

    # Commit vs session drift
    commit_drift: str            # what was discussed vs what was actually shipped

    # Delta analysis vs yesterday's Reflection
    config_changes_observed: list[str]   # configs that look different from yesterday
    friction_observed: list[str]         # friction patterns seen today
    yesterday_recs_followed: list[str]   # recommendations that appear to have been acted on
    yesterday_recs_ignored: list[str]    # recommendations not followed
    model_usage_observed: list[str]      # e.g. ["claude-code: claude-sonnet-4-6", "cursor: gpt-4o"]

    # Free-form day read (feeds Synthesizer as narrative context)
    day_summary: str


# ── Final output: Reflection ────────────────────────────────────────────────────

class Reflection(BaseModel):
    date: str

    # Mind map
    mind_map_nodes: list[MindMapNode]
    mind_map_edges: list[tuple[str, str]]

    # Your side
    your_mood: MoodSignal
    your_question: ImplicitQuestion

    # Agent side
    agent_mood: MoodSignal
    agent_question: ImplicitQuestion

    # The one takeaway worth remembering a month from now
    insight: str = ""

    # Alignment
    alignment_score: float = Field(ge=0.0, le=1.0)
    alignment_label: Literal["aligned", "mild-friction", "friction", "misaligned"]
    friction_points: list[FrictionPoint]
    recommendations: list[Recommendation]

    # Delta layer — what changed from yesterday
    config_diffs: list[ConfigDiff]
    friction_deltas: list[FrictionDelta]
    model_usage: list[ModelUsage]
    acted_on_recommendations: list[str]   # which of yesterday's recs were followed

    # History + voice context
    seven_day_pattern: list[DayPattern]
    synthesis_context: str                # 2-3 paragraph voice briefing
