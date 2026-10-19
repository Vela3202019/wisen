"""
Packets: the only shapes allowed to leave this machine. wisen/egress.py checks
every outbound packet against these models; a future server validates the same
models on receipt, so the schema is the privacy boundary at both ends.

  result       a reflection, as the dashboard and phone show it (CloudReflection)
  observation  the Observer's notes on a day, for a server-side Synthesizer
  usage        subscription usage percentages and reset times

Builders here only shape data. Redaction and length caps live on the field
types (wisen/synthesis/schema.py) and run under the EGRESS validation context,
which the egress gate always applies. Every packet carries the user id, so
nothing assumes a single user.
"""
from typing import Literal

from pydantic import BaseModel, Field

from wisen import config
from wisen.synthesis.schema import (
    EGRESS,
    CloudKeyMoment,
    CloudReflection,
    Date,
    DayObservation,
    Label,
    Long,
    Text,
)

SCHEMA_VERSION = 1


def user_id() -> str:
    return config.get("storage", "user_id", "WISEN_USER_ID", "local")


class ResultPacket(CloudReflection):
    # Envelope fields sit beside the reflection fields so the dashboard reads
    # a result packet as a plain reflection.
    packet_type: Literal["result"]
    schema_version: Literal[1]
    user_id: Label


class ObservationPacket(BaseModel):
    packet_type: Literal["observation"]
    schema_version: Literal[1]
    user_id: Label
    date: Date
    mood_signals_user: Long
    mood_signals_agent: Long
    topics_raw: list[Label] = Field(max_length=100)
    key_moments: list[CloudKeyMoment] = Field(max_length=50)
    commit_drift: Long
    config_changes_observed: list[Text] = Field(max_length=50)
    friction_observed: list[Text] = Field(max_length=50)
    yesterday_recs_followed: list[Text] = Field(max_length=50)
    yesterday_recs_ignored: list[Text] = Field(max_length=50)
    model_usage_observed: list[Label] = Field(max_length=50)
    day_summary: Long


class ClaudeUsage(BaseModel):
    five_hour_pct: float | None = None
    five_hour_resets_at: Label | None = None
    seven_day_pct: float | None = None
    seven_day_resets_at: Label | None = None


class CodexUsage(BaseModel):
    plan: Label | None = None
    as_of: Label | None = None
    five_hour_pct: float | None = None
    five_hour_resets_at: Label | None = None
    weekly_pct: float | None = None
    weekly_resets_at: Label | None = None


class UsagePacket(BaseModel):
    packet_type: Literal["usage"]
    schema_version: Literal[1]
    user_id: Label
    collected_at: Label
    claude: ClaudeUsage | None
    codex: CodexUsage | None


PACKETS: dict[str, type[BaseModel]] = {
    "result": ResultPacket,
    "observation": ObservationPacket,
    "usage": UsagePacket,
}


def _envelope(packet_type: str) -> dict:
    return {"packet_type": packet_type, "schema_version": SCHEMA_VERSION, "user_id": user_id()}


def result_packet(doc: dict) -> dict:
    """A stored reflection document, cut down to the CloudReflection allow-list."""
    reflection = CloudReflection.model_validate(doc, context=EGRESS)
    return {**_envelope("result"), **reflection.model_dump(mode="json")}


def observation_packet(observation: DayObservation, date: str) -> dict:
    return {**_envelope("observation"), "date": date, **observation.model_dump(mode="json")}


def usage_packet(usage: dict) -> dict:
    return {**_envelope("usage"), **usage}
