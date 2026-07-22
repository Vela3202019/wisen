"""
Test the eval case miner and Tier-1 metrics against synthetic transcripts.
Run: python3 -m tests.test_evals
"""
import json
import tempfile
from pathlib import Path

from evals.cases import Case, ContextTurn, assign_split, load_cases, save_case
from evals.metrics import daily_correction_rates, friction_half_life, pearson
from evals.miner import scan_all
from evals.replay import build_prompt


def _event(role: str, text, ts: str, session="s1", **extra) -> dict:
    return {
        "type": role, "sessionId": session, "timestamp": ts,
        "message": {"role": role, "content": text}, **extra,
    }


def _write_transcript(claude_dir: Path, events: list[dict],
                      project="-Users-me-proj", name="t.jsonl") -> None:
    d = claude_dir / project
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("\n".join(json.dumps(e) for e in events))


def _scan(claude_dir: Path):
    return list(scan_all(claude_dir))


def test_detectors():
    with tempfile.TemporaryDirectory() as tmp:
        claude_dir = Path(tmp)
        rejection_block = [{
            "type": "tool_result",
            "content": "The user doesn't want to proceed with this tool use. The tool use was rejected.",
        }]
        _write_transcript(claude_dir, [
            _event("user", "build me a fastapi app", "2026-07-01T10:00:00Z"),
            _event("assistant", "sure, writing it now", "2026-07-01T10:00:10Z"),
            _event("user", rejection_block, "2026-07-01T10:01:30Z"),                       # tool-rejection
            _event("user", "[Request interrupted by user]", "2026-07-01T10:05:00Z"),       # interruption
            _event("assistant", "ok, stopped", "2026-07-01T10:05:10Z"),
            _event("user", "no, that's not what I meant", "2026-07-01T10:08:00Z"),         # explicit-correction
            _event("assistant", "understood", "2026-07-01T10:08:10Z"),
            _event("user", "add the endpoint", "2026-07-01T10:20:00Z"),
            _event("user", "add the endpoint to main.py I mean", "2026-07-01T10:20:30Z"),  # rephrase-storm
        ])
        scans = _scan(claude_dir)
        assert len(scans) == 1, f"expected 1 session, got {len(scans)}"
        detectors = [c.detector for c in scans[0].cases]
        assert detectors == [
            "tool-rejection", "interruption", "explicit-correction", "rephrase-storm",
        ], f"unexpected detections: {detectors}"
        assert scans[0].user_turns == 6
        # context is the turns before the moment, correction is verbatim
        explicit = scans[0].cases[2]
        assert explicit.correction == "no, that's not what I meant"
        assert explicit.context[-1].content == "ok, stopped"
        print(f"✓ detectors: {detectors}")


def test_no_false_positives():
    with tempfile.TemporaryDirectory() as tmp:
        claude_dir = Path(tmp)
        _write_transcript(claude_dir, [
            _event("user", "now add tests please", "2026-07-01T10:00:00Z"),      # "now" != "no"
            _event("assistant", "done", "2026-07-01T10:05:00Z"),
            _event("user", "looks good, ship it", "2026-07-01T10:10:00Z"),
            _event("user", "no wait", "2026-07-01T10:10:20Z", isSidechain=True), # sidechains skipped
            _event("user", "meta", "2026-07-01T10:10:30Z", isMeta=True),         # meta skipped
            _event("user", "<task-notification> <task-id>x</task-id>", "2026-07-01T10:10:40Z"),  # harness msg
            _event("user", "Caveat: the messages below were generated", "2026-07-01T10:10:50Z"),
        ])
        scans = _scan(claude_dir)
        assert len(scans) == 1 and scans[0].cases == [], \
            f"false positives: {[c.detector for s in scans for c in s.cases]}"
        print("✓ no false positives on clean session")


def test_case_bank_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        cases_dir = Path(tmp)
        case = Case(
            id="2026-07-01-abc12345-3", date="2026-07-01", session_id="abc12345",
            source_path="/x.jsonl", project_path="/p", detector="interruption",
            context=[], correction="[Request interrupted by user]",
            split=assign_split("2026-07-01"),
        )
        save_case(case, cases_dir)
        loaded = load_cases(cases_dir)
        assert len(loaded) == 1 and loaded[0] == case
        assert assign_split("2026-07-01") == assign_split("2026-07-01")  # deterministic
        print(f"✓ case bank roundtrip (split={case.split})")


def test_metrics():
    with tempfile.TemporaryDirectory() as tmp:
        claude_dir = Path(tmp)
        _write_transcript(claude_dir, [
            _event("user", "do the thing", "2026-07-01T10:00:00Z"),
            _event("assistant", "done", "2026-07-01T10:01:00Z"),
            _event("user", "no, revert that", "2026-07-01T10:02:00Z"),
        ])
        rates = daily_correction_rates(claude_dir)
        day = rates["2026-07-01"]
        assert day["user_turns"] == 2 and day["corrections"] == 1
        assert day["per_100_turns"] == 50.0
        assert day["by_detector"] == {"explicit-correction": 1}

    logs = {
        "2026-07-01": {"friction_deltas": [
            {"status": "resolved", "days_persisting": 2},
            {"status": "persisting", "days_persisting": 5},
            {"status": "resolved", "days_persisting": 4},
        ]},
    }
    assert friction_half_life(logs) == 3.0
    assert pearson([1, 2, 3], [2, 4, 6]) == 1.0
    assert pearson([1, 2, 3], [6, 4, 2]) == -1.0
    assert pearson([1, 2], [2, 4]) is None  # too few points
    print("✓ metrics: rates, half-life, pearson")


def test_build_prompt():
    case = Case(
        id="x", date="2026-07-01", session_id="s", source_path="/x", project_path="/p",
        detector="explicit-correction", correction="no", split="eval",
        context=[
            ContextTurn(role="user", content="add an endpoint"),
            ContextTurn(role="assistant", content="ok, thinking"),
            ContextTurn(role="user", content="make it POST /habits"),
            ContextTurn(role="assistant", content="writing it with a fallback default"),
        ],
    )
    prompt = build_prompt(case)
    # trailing assistant turns dropped; last user instruction is the ask
    assert prompt.endswith("make it POST /habits")
    assert "fallback default" not in prompt
    assert "add an endpoint" in prompt  # earlier context preserved
    print("✓ build_prompt drops trailing agent turns, keeps earlier context")


if __name__ == "__main__":
    test_detectors()
    test_no_false_positives()
    test_case_bank_roundtrip()
    test_metrics()
    test_build_prompt()
    print("\nall eval tests passed")
