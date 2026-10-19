"""
The egress gate: every byte wisen sends off this machine goes through here.

A packet must name a known packet type (wisen/packets.py) and validate against
it with no extra fields at any depth; its text is redacted and length-capped on
the way through. Anything else is rejected.

Each packet that passes is recorded in the egress log (~/.wisen/egress.log,
WISEN_EGRESS_LOG) as one JSON line: time, destination, packet type, size, field
list and sha256 of the bytes. The content itself is never logged. `wisen
egress` reads it back.

Today the only transport is a directory that a sync job outside wisen (e.g.
rsync to the dashboard) uploads. Such entries are logged when the packet is
written there, as "outbox:<file>", i.e. prepared for upload; the upload itself
is not observed.
"""
import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from wisen import config
from wisen.packets import PACKETS
from wisen.synthesis.schema import EGRESS


class EgressRejected(ValueError):
    pass


def log_path() -> Path:
    return Path(config.get("paths", "egress_log", "WISEN_EGRESS_LOG", "~/.wisen/egress.log")).expanduser()


def check(data: dict) -> BaseModel:
    """Validate, redact and cap one packet, or raise EgressRejected."""
    model = PACKETS.get(data.get("packet_type"))
    if model is None:
        raise EgressRejected("not a packet: needs a known packet_type")
    try:
        return model.model_validate(data, extra="forbid", context=EGRESS)
    except ValidationError as e:
        # include_input=False: the rejected values may be what we are protecting
        problems = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}"
                             for err in e.errors(include_input=False))
        raise EgressRejected(f"{data['packet_type']} packet rejected: {problems}") from None


def write_file(data: dict, path: Path) -> bool:
    """Put a packet in an outbox directory that a sync job uploads (the
    dashboard export dir).

    The file is left alone when its content is unchanged, so a sync tool sends
    nothing new and the log records only packets that actually changed.
    Returns whether the file was written.
    """
    packet = check(data)
    body = packet.model_dump_json(indent=2)
    if path.exists() and path.read_text() == body:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    _record(packet, body.encode(), f"outbox:{_home_relative(path)}")
    return True


def _home_relative(path: Path) -> str:
    path = path.resolve()
    try:
        return f"~/{path.relative_to(Path.home().resolve())}"
    except ValueError:
        return str(path)


def _record(packet: BaseModel, body: bytes, destination: str) -> None:
    entry = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "destination": destination,
        "packet_type": packet.packet_type,
        "bytes": len(body),
        "fields": list(type(packet).model_fields),
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def read_log(day: date) -> list[dict]:
    """Log entries for one local calendar day, oldest first."""
    entries = []
    try:
        lines = log_path().read_text().splitlines()
    except FileNotFoundError:
        return []
    for line in lines:
        try:
            entry = json.loads(line)
            at = datetime.fromisoformat(entry["at"]).astimezone()
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        if at.date() == day:
            entries.append({**entry, "at": at})
    return entries


def report(day: date, every: bool = False) -> str:
    """What passed the gate on `day`: per destination, or every packet."""
    entries = read_log(day)
    total = sum(e["bytes"] for e in entries)
    lines = [f"{len(entries)} packets, {total:,} bytes passed the egress gate on {day} (log: {log_path()})",
             "  outbox:<file> = written for a sync job to upload"]
    if every:
        for e in entries:
            lines.append(f"  {e['at']:%H:%M:%S}  {e['packet_type']:<11} {e['bytes']:>7,} B  "
                         f"sha256:{e['sha256'][:12]}  {e['destination']}")
        return "\n".join(lines)
    groups: dict[str, list[dict]] = {}
    for e in entries:
        groups.setdefault(e["destination"], []).append(e)
    for destination, sent in groups.items():
        last = sent[-1]
        lines.append(f"\n  {destination}")
        lines.append(f"    {len(sent)} × {last['packet_type']}, {sum(e['bytes'] for e in sent):,} bytes, "
                     f"last at {last['at']:%H:%M:%S} (sha256:{last['sha256'][:12]})")
        lines.append(f"    fields: {', '.join(last['fields'])}")
    return "\n".join(lines)
