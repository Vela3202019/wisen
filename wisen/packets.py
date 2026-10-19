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
nothing assumes a single user, a packet_id (sha256 of the packet's content
after redaction, so rebuilding the same packet gives the same id) and its
created_at time.
"""
import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, ValidationError

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

PacketId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$", max_length=64)]


class EgressRejected(ValueError):
    pass


def user_id() -> str:
    return config.get("storage", "user_id", "WISEN_USER_ID", "local")


class Envelope(BaseModel):
    schema_version: Literal[1]
    user_id: Label
    packet_id: PacketId
    created_at: Label


class ResultPacket(CloudReflection, Envelope):
    # Envelope fields sit beside the reflection fields so the dashboard reads
    # a result packet as a plain reflection.
    packet_type: Literal["result"]


class CloudObservation(BaseModel):
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


class ObservationPacket(CloudObservation, Envelope):
    packet_type: Literal["observation"]


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


class Usage(BaseModel):
    collected_at: Label
    claude: ClaudeUsage | None
    codex: CodexUsage | None


class UsagePacket(Usage, Envelope):
    packet_type: Literal["usage"]


PACKETS: dict[str, type[BaseModel]] = {
    "result": ResultPacket,
    "observation": ObservationPacket,
    "usage": UsagePacket,
}


def validate(model: type[BaseModel], data: dict, name: str, **kwargs) -> BaseModel:
    """Validate, redact and cap under the EGRESS context, or raise EgressRejected."""
    try:
        return model.model_validate(data, context=EGRESS, **kwargs)
    except ValidationError as e:
        # include_input=False: the rejected values may be what we are protecting
        problems = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}"
                             for err in e.errors(include_input=False))
        raise EgressRejected(f"{name} rejected: {problems}") from None


def _packet(packet_type: str, content: BaseModel) -> dict:
    body = {"packet_type": packet_type, "schema_version": SCHEMA_VERSION, "user_id": user_id(),
            **content.model_dump(mode="json")}
    packet_id = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return {**body, "packet_id": packet_id, "created_at": datetime.now(UTC).isoformat(timespec="seconds")}


def result_packet(doc: dict) -> dict:
    """A stored reflection document, cut down to the CloudReflection allow-list."""
    return _packet("result", validate(CloudReflection, doc, "result packet"))


def observation_packet(observation: DayObservation, date: str) -> dict:
    content = {"date": date, **observation.model_dump(mode="json")}
    return _packet("observation", validate(CloudObservation, content, "observation packet", extra="forbid"))


def usage_packet(usage: dict) -> dict:
    return _packet("usage", validate(Usage, usage, "usage packet", extra="forbid"))
