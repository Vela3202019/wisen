"""
Packets: the only shapes allowed to leave this machine. wisen/egress.py checks
every outbound packet against these models; a future server validates the same
models on receipt, so the schema is the privacy boundary at both ends.

  result       a reflection, as the dashboard and phone show it (CloudReflection)
  observation  the Observer's notes on a day, for a server-side Synthesizer
  usage        subscription usage percentages and reset times
  proposal     a Gardener proposal awaiting a human decision
  proposal_update  a git state the laptop decided for a proposal (applied,
               merged, measured, …), with its branch and measured numbers
  metrics      one day's verification-loop numbers (no text)
  job_status   how a device job ended: state, timings, error class

Builders here only shape data. Redaction and length caps live on the field
types (wisen/synthesis/schema.py) and run under the EGRESS validation context,
which the egress gate always applies. Every packet carries the user id, so
nothing assumes a single user, a packet_id (sha256 of the packet's content
after redaction, so rebuilding the same packet gives the same id) and its
created_at time. A packet bound for a wisen server also carries the sending
device's id.

`receive` is the server's side of the same boundary (wisen/cloud): the same
models, but nothing is redacted or cut there; a packet that would need either
is rejected.
"""
import hashlib
import json
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    StringConstraints,
    ValidationError,
    ValidationInfo,
)

from wisen import config
from wisen.redact import redact
from wisen.synthesis.schema import (
    EGRESS,
    CloudKeyMoment,
    CloudReflection,
    Date,
    DayObservation,
    Label,
    Long,
    Quote,
    Text,
)

SCHEMA_VERSION = 1

PacketId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$", max_length=64)]
HexId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$", max_length=32)]


class EgressRejected(ValueError):
    pass


class PacketRejected(ValueError):
    """The receiving end refused a packet. The message names fields, never values."""


def _whole(max_len: int):
    """A value that must leave whole or not at all: a diff, a path. Under the
    EGRESS context a value that redaction would change, or that is over its
    cap, rejects the packet instead of being rewritten or cut (a human never
    approves a cut-short diff)."""
    def check(value, info: ValidationInfo):
        if isinstance(value, str) and (info.context or {}).get("egress"):
            if redact(value) != value:
                raise ValueError("holds text the egress gate would redact; a whole value is never rewritten")
            if len(value) > max_len:
                raise ValueError(f"is over its {max_len}-character cap; a whole value is never cut")
        return value
    return Annotated[str, BeforeValidator(check), StringConstraints(max_length=max_len)]


Target = _whole(300)       # project alias + relative path of a file the Gardener may edit
Diff = _whole(20_000)      # a unified diff
ErrorClass = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][\w.]*$", max_length=200)]
Branch = Annotated[str, StringConstraints(pattern=r"^wisen/gardener/[0-9a-f]{32}$", max_length=60)]
UpdateId = Annotated[str, StringConstraints(
    pattern=r"^(cli|expire|applied|failed|merged|abandoned|measuring|measured):[0-9a-f]{32}$", max_length=50)]


def user_id() -> str:
    return config.get("storage", "user_id", "WISEN_USER_ID", "local")


class Envelope(BaseModel):
    schema_version: Literal[1]
    user_id: Label
    device_id: Label | None = None  # set on packets sent to a wisen server
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


class Decided(BaseModel):
    """What became of a proposal the laptop decided or expired while unpaired.
    It travels with the proposal's first upload: the server takes no device
    decision for a proposal it already holds."""
    request_id: UpdateId
    state: Literal["approved", "rejected", "expired"]
    at: Label
    note: Text | None = None


class CloudProposal(BaseModel):
    proposal_id: HexId
    target: Target
    diff: Diff
    rationale: Text
    evidence: list[Quote] = Field([], max_length=10)
    eval_summary: Text | None = None
    expires_at: Label | None = None
    batch: Date | None = None   # the day whose Gardener run made it (the daily batch)
    decided: Decided | None = None


class ProposalPacket(CloudProposal, Envelope):
    packet_type: Literal["proposal"]


class MeasureWindow(BaseModel):
    first: Date
    last: Date
    user_turns: int
    problems_solved: int
    corrections_per_100_turns: float | None
    tokens_per_problem: float | None
    hitl_turns_per_pr: float | None
    friction_half_life_days: float | None


class CloudMeasure(BaseModel):
    verdict: Literal["helped", "no_change", "worse"]
    before: MeasureWindow
    after: MeasureWindow


class ProposalUpdate(BaseModel):
    """A move the laptop is the authority for (cloud-architecture §6.1)."""
    proposal_id: HexId
    request_id: UpdateId
    from_state: Literal["approved", "applied", "merged", "measuring"]
    to_state: Literal["applied", "failed", "merged", "abandoned", "measuring", "kept", "revert_proposed"]
    at: Label
    branch: Branch | None = None
    note: Text | None = None
    measure: CloudMeasure | None = None


class ProposalUpdatePacket(ProposalUpdate, Envelope):
    packet_type: Literal["proposal_update"]


class Detections(BaseModel):
    tool_rejection: int
    interruption: int
    explicit_correction: int
    rephrase_storm: int


class DayMetrics(BaseModel):
    """The verification loop's numbers for one day (wisen/evals/verification.py)."""
    date: Date
    user_turns: int
    corrections: int
    corrections_per_100_turns: float | None
    by_detector: Detections
    tokens: int
    cost_usd: float | None
    problems_solved: int
    tokens_per_problem: float | None
    turns_per_problem: float | None
    cost_per_problem: float | None
    hitl_turns_per_pr: float | None
    reverted: int
    refixed: int


class MetricsPacket(DayMetrics, Envelope):
    packet_type: Literal["metrics"]


class JobStatus(BaseModel):
    job_id: HexId
    kind: Label
    state: Literal["succeeded", "failed"]
    started_at: Label
    finished_at: Label
    error_class: ErrorClass | None = None  # an exception's class name, never its message or traceback
    skipped: Text | None = None            # a synthesize job that left the day alone, and why


class JobStatusPacket(JobStatus, Envelope):
    packet_type: Literal["job_status"]


PACKETS: dict[str, type[BaseModel]] = {
    "result": ResultPacket,
    "observation": ObservationPacket,
    "usage": UsagePacket,
    "proposal": ProposalPacket,
    "proposal_update": ProposalUpdatePacket,
    "metrics": MetricsPacket,
    "job_status": JobStatusPacket,
}


def validate(model: type[BaseModel], data: dict, name: str, **kwargs) -> BaseModel:
    """Validate, redact and cap under the EGRESS context, or raise EgressRejected."""
    try:
        return model.model_validate(data, context=EGRESS, **kwargs)
    except ValidationError as e:
        raise EgressRejected(f"{name} rejected: {_problems(e)}") from None


def _problems(e: ValidationError) -> str:
    # include_input=False: the rejected values may be what we are protecting
    return "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors(include_input=False))


def content_id(body: dict) -> str:
    """The packet_id: sha256 of everything but the id and the send time. A
    packet without a device leaves `device_id` out, so ids made before devices
    existed stay the same."""
    body = {k: v for k, v in body.items() if k not in ("packet_id", "created_at")}
    if body.get("device_id") is None:
        body.pop("device_id", None)
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _packet(packet_type: str, content: BaseModel, user: str | None, device_id: str | None) -> dict:
    body = {"packet_type": packet_type, "schema_version": SCHEMA_VERSION, "user_id": user or user_id(),
            **content.model_dump(mode="json")}
    if device_id is not None:
        body["device_id"] = device_id
    return {**body, "packet_id": content_id(body), "created_at": datetime.now(UTC).isoformat(timespec="seconds")}


# The builders take the user and device a wisen server knows this machine by;
# without them the packet carries the local user id and no device.

def result_packet(doc: dict, *, user: str | None = None, device_id: str | None = None) -> dict:
    """A stored reflection document, cut down to the CloudReflection allow-list."""
    return _packet("result", validate(CloudReflection, doc, "result packet"), user, device_id)


def observation_packet(observation: DayObservation, date: str) -> dict:
    content = {"date": date, **observation.model_dump(mode="json")}
    return _packet("observation", validate(CloudObservation, content, "observation packet", extra="forbid"),
                   None, None)


def usage_packet(usage: dict) -> dict:
    return _packet("usage", validate(Usage, usage, "usage packet", extra="forbid"), None, None)


def proposal_packet(proposal_id: str, target: str, payload: dict, expires_at: str | None, *,
                    decided: dict | None = None, user: str | None = None, device_id: str | None = None) -> dict:
    """A stored proposal, cut down to the CloudProposal allow-list: the rest of
    its payload (a paused agent run, say) stays here."""
    content = {"proposal_id": proposal_id, "target": target, "expires_at": expires_at, "decided": decided,
               **{k: payload[k] for k in ("diff", "rationale", "evidence", "eval_summary", "batch") if k in payload}}
    return _packet("proposal", validate(CloudProposal, content, "proposal packet", extra="forbid"), user, device_id)


def proposal_update_packet(update: dict, *, user: str | None = None, device_id: str | None = None) -> dict:
    return _packet("proposal_update", validate(ProposalUpdate, update, "proposal update packet", extra="forbid"),
                   user, device_id)


def metrics_packet(doc: dict, *, user: str | None = None, device_id: str | None = None) -> dict:
    return _packet("metrics", validate(DayMetrics, doc, "metrics packet", extra="forbid"), user, device_id)


def job_status_packet(status: dict, *, user: str | None = None, device_id: str | None = None) -> dict:
    return _packet("job_status", validate(JobStatus, status, "job status packet", extra="forbid"), user, device_id)


def receive(data: object, accepted: Collection[str]) -> BaseModel:
    """The server's check on a packet off the wire, or PacketRejected.

    The same strict models as the egress gate, but nothing is redacted or cut
    here: an unknown field, an over-cap value, or text the gate would have
    redacted rejects the packet, so a laptop-side bug cannot widen the
    boundary. The packet_id must match the content, so a stored packet is
    exactly what its id says."""
    packet_type = data.get("packet_type") if isinstance(data, dict) else None
    model = PACKETS.get(packet_type) if isinstance(packet_type, str) else None
    if model is None or packet_type not in accepted:
        raise PacketRejected(f"not a packet this server accepts; accepted types: {', '.join(sorted(accepted))}")
    try:
        packet = model.model_validate(data, extra="forbid")
    except ValidationError as e:
        raise PacketRejected(f"{packet_type} packet rejected: {_problems(e)}") from None
    try:
        scrubbed = validate(model, data, f"{packet_type} packet", extra="forbid")
    except EgressRejected as e:
        raise PacketRejected(str(e)) from None
    if scrubbed.model_dump() != packet.model_dump():
        changed = [k for k in type(packet).model_fields if getattr(scrubbed, k) != getattr(packet, k)]
        raise PacketRejected(f"{packet_type} packet rejected: unredacted text in {', '.join(changed)}")
    if content_id(packet.model_dump(mode="json")) != packet.packet_id:
        raise PacketRejected(f"{packet_type} packet rejected: packet_id does not match its content")
    return packet
