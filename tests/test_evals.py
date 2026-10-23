"""
Test the eval case miner and Tier-1 metrics against synthetic transcripts.
Run: python3 -m tests.test_evals
"""
import asyncio
import json
import tempfile
from pathlib import Path

from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from wisen.evals import cli as evals_cli
from wisen.evals import judge, miner, runner
from wisen.evals.cases import Case, ContextTurn, assign_split, load_cases, save_case
from wisen.evals.metrics import daily_correction_rates, friction_half_life, pearson
from wisen.evals.miner import scan_all
from wisen.evals.replay import ReplayResult, build_prompt


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


def test_clean_set():
    """A session with no detection yields one clean case; its fixed
    behavior check needs no model, and once confirmed it makes the replay's
    clean-set rate real."""
    with tempfile.TemporaryDirectory() as tmp:
        claude_dir, cases_dir = Path(tmp) / "projects", Path(tmp) / "cases"
        _write_transcript(claude_dir, [
            _event("user", "add a health endpoint", "2026-07-01T10:00:00Z", session="smooth01"),
            _event("assistant", "added GET /health", "2026-07-01T10:02:00Z", session="smooth01"),
            _event("user", "now add a test for it", "2026-07-01T10:10:00Z", session="smooth01"),
            _event("assistant", "test added", "2026-07-01T10:12:00Z", session="smooth01"),
        ], name="smooth.jsonl")
        _write_transcript(claude_dir, [
            _event("user", "add a health endpoint", "2026-07-01T11:00:00Z", session="rough001"),
            _event("assistant", "added it with a fallback", "2026-07-01T11:02:00Z", session="rough001"),
            _event("user", "no, no fallback", "2026-07-01T11:10:00Z", session="rough001"),
        ], name="rough.jsonl")
        _write_transcript(claude_dir, [
            _event("user", "hi", "2026-07-01T12:00:00Z", session="single01"),
            _event("assistant", "hello", "2026-07-01T12:00:10Z", session="single01"),
        ], name="single.jsonl")
        scans = {s.session_id: s for s in scan_all(claude_dir)}
        assert scans["rough001"].clean is None and scans["rough001"].cases   # a correction, not clean
        assert scans["single01"].clean is None                                 # nothing before its only turn
        clean = scans["smooth01"].clean
        assert clean.kind == "clean" and clean.detector is None and clean.status == "mined", clean
        assert clean.correction == "now add a test for it"
        assert [t.content for t in clean.context] == ["add a health endpoint", "added GET /health"]
        assert clean.behavior_check == miner.CLEAN_CHECK

        # `mine` saves it without the LLM labeling pass (that pass is for corrections).
        async def no_model(case):
            raise AssertionError(f"enrich called for {case.id}")

        real = (miner.scan_all, miner.enrich, evals_cli.save_case, evals_cli.case_path)
        miner.scan_all = lambda since=None: (s for s in scan_all(claude_dir, since) if s.clean)
        miner.enrich = no_model
        evals_cli.save_case = lambda case: save_case(case, cases_dir)
        evals_cli.case_path = lambda case_id: cases_dir / f"{case_id}.json"
        try:
            asyncio.run(evals_cli._mine(None, no_enrich=False))
        finally:
            miner.scan_all, miner.enrich, evals_cli.save_case, evals_cli.case_path = real
        (loaded,) = load_cases(cases_dir)
        assert loaded == clean, loaded

        loaded.status, loaded.split = "confirmed", "eval"

        async def fake(case, variant, k):
            return {"case": case.id, "passed": variant.claude_md is None, "tokens": 10, "cost_usd": None}

        report = asyncio.run(runner.compare([loaded], runner.Variant(), runner.Variant(claude_md=Path("x")),
                                            cap=2, run=fake))
        assert report["cases"] == {"eval": 0, "clean": 1}, report
        assert report["current"]["clean_pass_rate"] == 1.0 and report["proposed"]["clean_pass_rate"] == 0.0
        print("✓ clean set: a session with no detection becomes a clean case; mine saves it unlabeled by a model")


def test_judge_prompt_by_kind():
    """The judge is told a clean case's message was the user carrying on,
    not a correction; a correction case keeps the correction framing."""
    seen = []

    def respond(messages, info):
        seen.append(messages[-1])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"repeated": False, "evidence": "x"})])

    real = judge.get_model
    judge.get_model = lambda: FunctionModel(respond)
    judge._judge.cache_clear()
    try:
        replay = ReplayResult(final_text="done", diff="")
        for kind in ("clean", "correction"):
            case = Case(id=kind, date="2026-07-01", session_id="s", source_path="/x", project_path="/p",
                        detector=None if kind == "clean" else "explicit-correction", context=[],
                        correction="now add a test for it", behavior_check="Did it break?", split="eval", kind=kind)
            asyncio.run(judge.judge_once(case, replay))
    finally:
        judge.get_model = real
        judge._judge.cache_clear()
    clean, correction = seen
    clean_prompt = clean.parts[0].content
    assert "went smoothly" in clean.instructions and "corrected the agent" not in clean.instructions
    assert "## User's next message in the original, smooth session\nnow add a test for it" in clean_prompt
    assert "correction" not in clean_prompt.lower()
    assert "corrected the agent" in correction.instructions
    assert "## Original correction (ground truth)\nnow add a test for it" in correction.parts[0].content
    print("✓ judge: a clean case is framed as the user's next message in a smooth session")


if __name__ == "__main__":
    test_detectors()
    test_no_false_positives()
    test_case_bank_roundtrip()
    test_metrics()
    test_build_prompt()
    test_clean_set()
    test_judge_prompt_by_kind()
    print("\nall eval tests passed")
