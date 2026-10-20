from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
)

from wisen.redact import redact

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

    # 3 scannable bullets, each "<emoji> <max 8 simple words>" (defaulted: old docs lack it)
    tldr: list[str] = []

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
    ignored_recommendations: list[str] = []  # yesterday's recs not acted on (defaulted: pre-existing docs lack it)

    # History + voice context
    seven_day_pattern: list[DayPattern]
    synthesis_context: str                # 2-3 paragraph voice briefing


# ── Cloud export: the allow-list of what may leave the machine ────────────────
# Remote dashboards get reflections only through this model (wrapped in a
# ResultPacket, wisen/packets.py). Fields are listed explicitly (not inherited
# from Reflection) so a new Reflection field stays local until someone decides
# it may egress. Anything else stored on a reflection document (synthesized_at,
# legacy or extra keys, Stage-1 output) is dropped. Short evidence quotes are
# synthesized results and stay in.
#
# Every text field is capped. Under the EGRESS validation context a value is
# redacted (secrets, emails, absolute paths) and then cut to fit; without it
# (the receiving end) an over-long value is rejected.

EGRESS = {"egress": True}


def _capped(max_len: int):
    def prepare(value, info: ValidationInfo):
        if isinstance(value, str) and (info.context or {}).get("egress"):
            value = redact(value)
            if len(value) > max_len:
                value = value[: max_len - 1] + "…"
        return value
    return Annotated[str, BeforeValidator(prepare), StringConstraints(max_length=max_len)]


Label = _capped(200)      # topics, sources, model names, file paths, tldr bullets
Quote = _capped(500)      # short evidence quotes
Text = _capped(1000)      # one-paragraph summaries, actions, questions
Long = _capped(4000)      # multi-paragraph briefings
Date = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$", max_length=10)]


class CloudMindMapNode(MindMapNode):
    topic: Label


class CloudMoodSignal(MoodSignal):
    summary: Text
    evidence: Quote


class CloudImplicitQuestion(ImplicitQuestion):
    question: Text
    evidence: Quote


class CloudFrictionPoint(FrictionPoint):
    description: Text
    evidence: Quote


class CloudRecommendation(Recommendation):
    action: Text
    reason: Text


class CloudDayPattern(DayPattern):
    date: Label


class CloudConfigDiff(ConfigDiff):
    file: Label
    summary: Text


class CloudFrictionDelta(FrictionDelta):
    description: Text


class CloudModelUsage(ModelUsage):
    source: Label
    model: Label


class CloudKeyMoment(KeyMoment):
    session_source: Label
    quote: Quote
    significance: Text


class CloudReflection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    date: Date
    mind_map_nodes: list[CloudMindMapNode] = Field(max_length=100)
    mind_map_edges: list[tuple[Label, Label]] = Field(max_length=200)
    your_mood: CloudMoodSignal
    your_question: CloudImplicitQuestion
    agent_mood: CloudMoodSignal
    agent_question: CloudImplicitQuestion
    insight: Text = ""
    tldr: list[Label] = Field([], max_length=10)
    alignment_score: float = Field(ge=0.0, le=1.0)
    alignment_label: Literal["aligned", "mild-friction", "friction", "misaligned"]
    friction_points: list[CloudFrictionPoint] = Field(max_length=50)
    recommendations: list[CloudRecommendation] = Field(max_length=50)
    config_diffs: list[CloudConfigDiff] = Field(max_length=50)
    friction_deltas: list[CloudFrictionDelta] = Field(max_length=50)
    model_usage: list[CloudModelUsage] = Field(max_length=50)
    acted_on_recommendations: list[Text] = Field(max_length=50)
    ignored_recommendations: list[Text] = Field([], max_length=50)
    seven_day_pattern: list[CloudDayPattern] = Field(max_length=31)
    synthesis_context: Long
