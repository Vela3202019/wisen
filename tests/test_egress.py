"""
Canary egress test: a reflection document carrying marker strings in fields that
are not on the CloudReflection allow-list is exported, and no marker may appear
in the JSON that leaves the machine. Allow-listed fields (evidence quotes too)
must round-trip. Also covers the egress gate: redaction, rejection of anything
off the packet schema, length caps and the content-free egress log.
No store needed.
Run: python3 -m tests.test_egress
"""
import asyncio
import contextlib
import hashlib
import io
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

_TMP = tempfile.mkdtemp()
os.environ["WISEN_EGRESS_LOG"] = str(Path(_TMP) / "egress.log")  # never the real log

from wisen import egress
from wisen.export import export_reflections
from wisen.packets import PACKETS, observation_packet, result_packet, usage_packet
from wisen.redact import redact
from wisen.synthesis.schema import CloudReflection, DayObservation, Reflection

ENVELOPE = {"packet_type", "schema_version", "user_id", "packet_id", "created_at"}
FAKE_KEY = "sk-ant-api03-Zx9Qw8Er7Ty6Ui5Op4As3Df2Gh1Jk0LmNb"
ABS_PATH = "/Users/alice/clients/acme/secret_plan.py"
EMAIL = "alice@acme-corp.com"

QUOTE = "why does the hook fire twice?"

MARKERS = [
    "CANARY_TOP_LEVEL_EXTRA",
    "CANARY_LEGACY_KEY",
    "CANARY_KEY_MOMENT_QUOTE",
    "CANARY_DAY_SUMMARY",
    "CANARY_RAW_METADATA",
    "CANARY_NESTED_EXTRA",
]


def _reflection() -> dict:
    mood = {"label": "iterating", "summary": "small loops", "evidence": QUOTE}
    question = {"question": "Is the hook registered twice?", "evidence": QUOTE}
    return {
        "date": "2026-10-17",
        "mind_map_nodes": [{"topic": "hooks", "weight": 0.8, "is_new": True}],
        "mind_map_edges": [["hooks", "settings"]],
        "your_mood": mood,
        "your_question": question,
        "agent_mood": {**mood, "label": "confident"},
        "agent_question": question,
        "insight": "One hook ran from two settings files.",
        "tldr": ["🔁 Hook ran twice"],
        "alignment_score": 0.7,
        "alignment_label": "mild-friction",
        "friction_points": [{"type": "config-conflict", "description": "Two hooks.", "evidence": QUOTE}],
        "recommendations": [{"target": "claude-md", "action": "Remove one hook.", "reason": "Duplicate."}],
        "config_diffs": [{"file": "~/.claude/settings.json", "change_type": "modified", "summary": "Added a hook."}],
        "friction_deltas": [{"description": "Two hooks.", "status": "new", "days_persisting": 0}],
        "model_usage": [{"source": "claude-code", "model": "claude-sonnet-4-6", "session_count": 3}],
        "acted_on_recommendations": [],
        "ignored_recommendations": [],
        "seven_day_pattern": [{"date": "2026-10-16", "alignment_score": 0.6, "alignment_label": "friction"}],
        "synthesis_context": "The hook ran twice because two files registered it.",
    }


def _poisoned_doc() -> dict:
    doc = _reflection()
    doc["_id"] = "CANARY_RAW_METADATA"
    doc["synthesized_at"] = datetime.now(UTC)
    doc["notes"] = "CANARY_TOP_LEVEL_EXTRA"
    doc["raw_transcript"] = "CANARY_LEGACY_KEY"
    doc["observation"] = {
        "key_moments": [{"session_source": "claude-code", "quote": "CANARY_KEY_MOMENT_QUOTE", "significance": "x"}],
        "day_summary": "CANARY_DAY_SUMMARY",
    }
    doc["key_moments"] = [{"session_source": "codex", "quote": "CANARY_KEY_MOMENT_QUOTE", "significance": "x"}]
    doc["your_mood"]["raw_prompt"] = "CANARY_NESTED_EXTRA"
    return doc


class _FakeStore:
    def __init__(self, docs):
        self._docs = docs

    async def list_reflections(self):
        return self._docs


def _export(docs) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        count = asyncio.run(export_reflections(_FakeStore(docs), Path(tmp)))
        assert count == len(docs)
        return "\n".join(p.read_text() for p in sorted(Path(tmp).glob("*.json")))


def test_no_marker_leaves():
    out = _export([_poisoned_doc()])
    leaked = [m for m in MARKERS if m in out]
    assert not leaked, f"leaked: {leaked}"
    assert "synthesized_at" not in out
    print("✓ egress: no canary marker in exported JSON")


def test_allow_listed_fields_round_trip():
    out = json.loads(_export([_poisoned_doc()]))
    assert {k: v for k, v in out.items() if k not in ENVELOPE} == _reflection()
    assert out["packet_type"] == "result" and out["schema_version"] == 1 and out["user_id"]
    assert out["friction_points"][0]["evidence"] == QUOTE
    print("✓ egress: allow-listed fields (incl. evidence quotes) round-trip")


def test_allow_list_is_reflection_fields():
    # Fails when Reflection gains a field: decide whether it may leave the machine,
    # then update CloudReflection (or this test, to keep it local).
    assert set(CloudReflection.model_fields) == set(Reflection.model_fields)
    print("✓ egress: CloudReflection lists exactly the Reflection result fields")


def _sensitive_doc() -> dict:
    doc = _reflection()
    doc["insight"] = f"Set ANTHROPIC_API_KEY={FAKE_KEY} before running {ABS_PATH}; ask {EMAIL}."
    doc["friction_points"][0]["evidence"] = f"why is {FAKE_KEY} rejected?"
    doc["config_diffs"][0]["file"] = "/Users/alice/.claude/settings.json"
    return doc


def test_sensitive_values_never_leave():
    out = _export([_sensitive_doc()])
    for value in (FAKE_KEY, "alice", "/Users/", EMAIL, "acme-corp"):
        assert value not in out, f"leaked: {value}"
    data = json.loads(out)
    assert "<secret>" in data["insight"] and "<email>" in data["insight"]
    assert "~/clients/acme/secret_plan.py" in data["insight"]
    assert data["config_diffs"][0]["file"] == "~/.claude/settings.json"
    print("✓ egress: fake API key, absolute paths and email are scrubbed")


def test_redact_patterns():
    cases = {
        f"key {FAKE_KEY} done": "key <secret> done",
        "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz1234": "OPENAI_API_KEY=<secret>",
        "token ghp_" + "a" * 36: "token <secret>",
        "AKIAIOSFODNN7EXAMPLE in env": "<secret> in env",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY----- after": "<secret> after",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNz": "<secret>",
        "Authorization: Bearer abcdefghijklmnopqrstuv.xyz": "Authorization: Bearer <secret>",
        'password: "hunter2hunter2"': 'password: "<secret>"',
        "postgres://u:pw123456@db.host/x": "postgres://<secret>@db.host/x",
        "https://u:pw123456@example.com/home/page/x": "https://<secret>@example.com/home/page/x",
        f"mail {EMAIL} now": "mail <email> now",
        f"edit {ABS_PATH}": "edit ~/clients/acme/secret_plan.py",
        "on /srv/acme/app/main.py": "on …/main.py",
        "Add /opt/homebrew/bin to PATH; 2>/dev/null": "Add /opt/homebrew/bin to PATH; 2>/dev/null",
        "https://github.com/Vela3202019/wisen/pull/4": "https://github.com/Vela3202019/wisen/pull/4",
        "see https://example.com/home/page/x": "see https://example.com/home/page/x",
        "open file:///Users/alice/notes": "open file://~/notes",
        "/System/Volumes/Data/Users/alice/secret.py": "/System/Volumes/Data~/secret.py",
        "/var/home/alice/proj/x.py": "/var~/proj/x.py",
        "/private/var/root/x": "/private/var~/x",
        "/usr/home/bob/a": "/usr~/a",
        "max_tokens=40000000 and tokens: 12345678 used": "max_tokens=40000000 and tokens: 12345678 used",
        "token: 12345678abc": "token: <secret>",
        "the bearer of bad news, and/or 1/2/3": "the bearer of bad news, and/or 1/2/3",
    }
    for text, expected in cases.items():
        assert redact(text) == expected, (text, redact(text))
        assert redact(expected) == expected, f"not idempotent: {expected}"
    print("✓ egress: redaction masks secrets, emails and paths, leaves prose alone")


def test_off_schema_is_rejected():
    packet = result_packet(_reflection())
    rejected = [
        {**packet, "raw_transcript": "CANARY_REJECTED"},                             # top-level field
        {**packet, "your_mood": {**packet["your_mood"], "raw": "CANARY_REJECTED"}},  # nested field
        {**packet, "packet_type": "transcript"},                        # unknown packet
        {**packet, "date": "../../etc/passwd"},                         # not a date
    ]
    for bad in rejected:
        try:
            egress.check(bad)
        except egress.EgressRejected as e:
            assert "CANARY_REJECTED" not in str(e) and "../../" not in str(e)
            continue
        raise AssertionError(f"accepted: {set(bad) - set(packet) or bad['packet_type']}")
    egress.check(packet)
    print("✓ egress: non-allow-listed fields and unknown packets are rejected")


def test_text_is_capped():
    packet = result_packet(_reflection())
    packet["synthesis_context"] = "a" * 10_000
    out = egress.check(packet)
    assert len(out.synthesis_context) == 4000 and out.synthesis_context.endswith("…")
    try:  # the receiving end rejects instead of truncating
        type(out).model_validate(packet)
    except ValueError:
        pass
    else:
        raise AssertionError("over-long text accepted without the egress context")
    print("✓ egress: text is truncated on the way out, rejected on the way in")


def test_every_text_field_has_a_max_length():
    def walk(node, where):
        if isinstance(node, dict):
            if node.get("type") == "string" and "enum" not in node and "const" not in node:
                assert "maxLength" in node, f"uncapped text field: {where}"
            for k, v in node.items():
                walk(v, f"{where}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{where}[{i}]")

    for name, model in PACKETS.items():
        walk(model.model_json_schema(), name)
    print("✓ egress: every text field in every packet has a max length")


def test_observation_and_usage_packets():
    obs = DayObservation(
        mood_signals_user=f"terse; pasted {FAKE_KEY}", mood_signals_agent="careful",
        topics_raw=["hooks"], key_moments=[{"session_source": "claude-code", "quote": QUOTE, "significance": "x"}],
        commit_drift="none", config_changes_observed=[], friction_observed=[ABS_PATH],
        yesterday_recs_followed=[], yesterday_recs_ignored=[], model_usage_observed=[],
        day_summary=f"Mailed {EMAIL}.",
    )
    out = egress.check(observation_packet(obs, "2026-10-17")).model_dump_json()
    for value in (FAKE_KEY, ABS_PATH, EMAIL):
        assert value not in out, f"leaked: {value}"
    assert QUOTE in out
    usage = {"collected_at": "2026-10-18T00:00:00+00:00", "claude": {"five_hour_pct": 12.0}, "codex": None}
    egress.check(usage_packet(usage))
    print("✓ egress: observation and usage packets pass the gate, scrubbed")


def test_packet_id_is_stable_content_hash():
    first, again = result_packet(_reflection()), result_packet(_reflection())
    assert len(first["packet_id"]) == 64 and first["packet_id"] == again["packet_id"]
    datetime.fromisoformat(first["created_at"])
    changed = _reflection()
    changed["insight"] = "Something else."
    assert result_packet(changed)["packet_id"] != first["packet_id"]
    obs = DayObservation(
        mood_signals_user="terse", mood_signals_agent="careful", topics_raw=[], key_moments=[],
        commit_drift="none", config_changes_observed=[], friction_observed=[],
        yesterday_recs_followed=[], yesterday_recs_ignored=[], model_usage_observed=[], day_summary="ok",
    )
    usage = {"collected_at": "2026-10-18T00:00:00+00:00", "claude": None, "codex": None}
    ids = {first["packet_id"], observation_packet(obs, "2026-10-17")["packet_id"], usage_packet(usage)["packet_id"]}
    assert len(ids) == 3
    # the id hashes the redacted content, so it reveals nothing the packet does not
    secret = _reflection()
    secret["insight"] = f"key {FAKE_KEY}"
    masked = _reflection()
    masked["insight"] = "key <secret>"
    assert result_packet(secret)["packet_id"] == result_packet(masked)["packet_id"]
    for packet in (first, observation_packet(obs, "2026-10-17"), usage_packet(usage)):
        for field in ("packet_id", "created_at"):
            try:
                egress.check({k: v for k, v in packet.items() if k != field})
            except egress.EgressRejected:
                continue
            raise AssertionError(f"{packet['packet_type']} accepted without {field}")
    print("✓ egress: every packet carries a stable content-hash packet_id and created_at")


def test_rejected_doc_is_skipped_without_its_text():
    bad = _reflection()
    bad["date"] = "2026-10-16"
    bad["tldr"] = [f"CANARY_REJECTED {i}" for i in range(11)]
    err = io.StringIO()
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(err):
        count = asyncio.run(export_reflections(_FakeStore([bad, _reflection()]), Path(tmp)))
        written = sorted(p.name for p in Path(tmp).glob("*.json"))
    assert count == 1 and written == ["2026-10-17.json"], (count, written)
    report = err.getvalue()
    assert "2026-10-16" in report and "tldr" in report and "CANARY_REJECTED" not in report, report
    print("✓ egress: a rejected reflection is skipped and reported without its text")


def test_log_records_sends_without_content():
    log = egress.log_path()
    log.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        db = _FakeStore([_sensitive_doc()])
        asyncio.run(export_reflections(db, Path(tmp)))
        body = (Path(tmp) / "2026-10-17.json").read_bytes()
        asyncio.run(export_reflections(db, Path(tmp)))  # unchanged: no second send
        assert (Path(tmp) / "2026-10-17.json").read_bytes() == body
        later = {**result_packet(_sensitive_doc()), "created_at": "2999-01-01T00:00:00+00:00"}
        assert not egress.write_file(later, Path(tmp) / "2026-10-17.json")  # only created_at differs
    lines = log.read_text().splitlines()
    assert len(lines) == 1, lines
    entry = json.loads(lines[0])
    assert entry["packet_type"] == "result" and entry["destination"].startswith("outbox:")
    assert entry["bytes"] == len(body) and entry["sha256"] == hashlib.sha256(body).hexdigest()
    assert set(entry["fields"]) == set(CloudReflection.model_fields) | ENVELOPE
    for content in (QUOTE, "One hook ran", "<secret>", "small loops"):
        assert content not in lines[0], f"content in log: {content}"
    today = datetime.now().astimezone().date()
    assert egress.report(today).startswith("1 packets, ")
    print("✓ egress: log records each send (hash, size, fields) without content")


if __name__ == "__main__":
    test_no_marker_leaves()
    test_allow_listed_fields_round_trip()
    test_allow_list_is_reflection_fields()
    test_sensitive_values_never_leave()
    test_redact_patterns()
    test_off_schema_is_rejected()
    test_text_is_capped()
    test_every_text_field_has_a_max_length()
    test_observation_and_usage_packets()
    test_packet_id_is_stable_content_hash()
    test_rejected_doc_is_skipped_without_its_text()
    test_log_records_sends_without_content()
    print("\nall egress tests passed")
