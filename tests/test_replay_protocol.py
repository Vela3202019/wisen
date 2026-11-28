"""Offline tests of the frozen protocol; no real agent or judge runs."""
import asyncio
import json
import os
import subprocess
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wisen.connectors.claude_code import _events_to_turns
from wisen.evals import calibration, exposure, miner, protocol, replay, runner
from wisen.evals.cases import Case, ContextTurn
from wisen.evals.judge import prompt_version
from wisen.evals.manifest import (
    FrozenCase,
    Manifest,
    ReplayFiles,
    freeze_proposal,
    load_manifest,
    save_manifest,
)
from wisen.gardener import applier
from wisen.gardener.git import Repo
from wisen.ingestion.normalizer import NormalizedSession, Turn, compute_metadata
from wisen.ingestion.pipeline import IngestResult
from wisen.packets import observation_packet
from wisen.store import SqliteStore
from wisen.synthesis import agent
from wisen.synthesis.schema import DayObservation, TurnCorrection

NOW = datetime(2026, 7, 15, 12, tzinfo=UTC)
SHA = "f" * 40


def case(name, cohort="targeted", project="/demo"):
    kind = "clean" if cohort == "clean" else "correction"
    data = Case(id=name, date="2026-07-01", session_id="session", source_path="/private",
                detector=None if kind == "clean" else "explicit-correction",
                project_path=str(project), context=[ContextTurn(role="user", content="Add test coverage")],
                correction="Say which test you added", behavior_check="Did the reply omit the test?",
                status="confirmed", split="holdout" if cohort == "holdout" else "eval", kind=kind)
    return FrozenCase(case=data, base_commit=SHA, cohort=cohort, rule_type="tests")


def manifest(*entries, proposal_id="proposal"):
    return Manifest(proposal_id=proposal_id, model="claude-sonnet-4-6", harness="1.0", judge_model="openai:stub",
                    judge_prompt=prompt_version(), current=ReplayFiles(project={"CLAUDE.md": "old"}),
                    proposed=ReplayFiles(project={"CLAUDE.md": "new"}), cases=list(entries))


async def fake_run(item, variant, votes):
    assert votes == 3
    wins = item.kind == "clean" or variant.project_files["CLAUDE.md"] == "new"
    return {"passed": wins, "output": {"final_text": "text", "diff": "diff"}}


def test_manifest_frozen_and_holdout(tmp_path):
    frozen = manifest(case("regression", "clean"), case("target"), case("hidden", "holdout"))
    path = tmp_path / "manifest.json"
    save_manifest(path, frozen)
    assert load_manifest(path).digest() == frozen.digest()
    assert path.stat().st_mode & 0o777 == 0o600
    saved = json.loads(path.read_text())
    saved["manifest"]["cases"][1]["case"]["context"][0]["content"] = "changed since freeze"
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="content hash"):
        load_manifest(path)
    report = asyncio.run(runner.run_manifest(frozen, run=fake_run))
    assert report["replays"] == 20 and report["meets_rule"] is True
    assert report["paired"]["eval"]["ci95"] == [1, 1]
    assert report["paired"]["clean"]["ci95"] == [0, 0]
    assert report["paired"]["eval"]["sign_p"] == 1
    private = asyncio.run(runner.run_manifest(frozen, holdout=True, run=fake_run))
    assert private["cases"] == {"holdout": 1} and private["meets_rule"] is None


def test_unknown_outcomes_do_not_create_a_gate_pass():
    async def backend(item, variant, votes):
        if item.id == "regression" and variant.project_files["CLAUDE.md"] == "new":
            return {"outcome": "abstain"}
        if item.id == "target" and variant.project_files["CLAUDE.md"] == "old":
            return {"error": "timeout"}
        return {"passed": True}

    frozen = manifest(case("regression", "clean"), case("target"))
    report = asyncio.run(runner.run_manifest(frozen, k=2, run=backend))
    assert report["meets_rule"] is None
    assert report["paired"]["clean"]["outcomes"]["proposed"]["abstain"] == 2
    assert report["paired"]["eval"]["outcomes"]["current"]["error"] == 2
    assert all(row["matched_scored"] == 0 for row in report["case_results"])
    assert report["proposed"]["clean_pass_rate"] == 0
    no_clean = asyncio.run(runner.run_manifest(manifest(case("target")), k=1, run=fake_run))
    assert no_clean["meets_rule"] is None


def _git(project: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(project), *args], text=True).strip()


def _git_setup(path, text):
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    (path / "CLAUDE.md").write_text(text)
    _git(path, "add", "CLAUDE.md")
    _git(path, "-c", "user.email=test@example.test", "-c", "user.name=test", "commit", "-qm", "config")
    return _git(path, "rev-parse", "HEAD")


def test_replay_uses_pinned_inputs_and_captures_new_files(tmp_path, monkeypatch):
    project, home, bin_dir = (tmp_path / part for part in ("project", "home", "bin"))
    head = _git_setup(project, "base instructions")
    home.mkdir()
    bin_dir.mkdir()
    (home / "settings.json").write_text("ambient settings")
    (home / "CLAUDE.md").write_text("ambient instructions")
    exe = bin_dir / "claude"
    exe.write_text("""#!/bin/sh
if [ "$1" = "--version" ]; then echo fake-1; exit 0; fi
test ! -e "$CLAUDE_CONFIG_DIR/settings.json" || exit 1
test "$(cat "$CLAUDE_CONFIG_DIR/CLAUDE.md")" = pinned-home || exit 1
test "$(cat CLAUDE.md)" = pinned-project || exit 1
printf 'new result\n' > result.txt
printf '{"result":"ok"}\n'
""")
    exe.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(replay, "_CLAUDE_HOME", home)
    item = case("target", project=project).case
    result = replay.run_replay(item, base_commit=head, harness="fake-1",
                               config_files={"CLAUDE.md": "pinned-home"}, project_files={"CLAUDE.md": "pinned-project"})
    assert not result.error
    assert "+new result" in result.diff and "pinned-project" not in result.diff
    assert not (project / "result.txt").exists()
    assert (project / "CLAUDE.md").read_text() == "base instructions"
    assert replay.run_replay(item, base_commit=head, harness="wrong").error


def _proposal(store, project, sha, before, after, proposal_id="proposal", at=NOW, *, path="CLAUDE.md", git_dir=""):
    with store.transaction() as conn:
        conn.execute("""
            INSERT INTO proposals (id, user_id, target, payload, state, created_at, updated_at)
            VALUES (?, ?, 'demo:CLAUDE.md', ?, 'merged', ?, ?)
        """, (proposal_id, store.user_id, json.dumps({"base_blob": before}), at.isoformat(), at.isoformat()))
        conn.execute("""
            INSERT INTO proposal_git (proposal_id, user_id, git_dir, work_tree, path, branch, base_commit,
                                      commit_sha, new_blob, applied_at, merged_at)
            VALUES (?, ?, ?, ?, ?, 'test', ?, ?, ?, ?, ?)
        """, (proposal_id, store.user_id, git_dir, str(project), path, sha, sha, after, at.isoformat(), at.isoformat()))


def test_freeze_gate_and_startup_exposure(tmp_path, monkeypatch):
    from wisen.evals import cases

    store = SqliteStore(tmp_path / "wisen.db", "local")
    project = tmp_path / "repo"
    head = _git_setup(project, "old")
    before = _git(project, "rev-parse", "HEAD:CLAUDE.md")
    (project / "CLAUDE.md").write_text("new")
    after = _git(project, "hash-object", "-w", "CLAUDE.md")
    _proposal(store, project, head, before, after)
    entries = [case("target", project=project).case, case("regression", "clean", project=project).case]
    monkeypatch.setattr(cases, "load_cases", lambda: entries)
    frozen = freeze_proposal(store, "proposal", [
        {"case_id": "target", "base_commit": head, "cohort": "targeted"},
        {"case_id": "regression", "base_commit": head, "cohort": "clean"},
    ], model="claude-sonnet-4-6", harness="fake", judge_model="openai:stub")
    assert frozen.current.project == {"CLAUDE.md": "old"} and frozen.proposed.blob == after
    report = asyncio.run(runner.run_manifest(frozen, k=1, run=fake_run))
    assert protocol.record_gate(store, frozen, report)["decision"] == "promote"
    assert protocol.evidence(store, "proposal")["observational_field"] is None
    assert exposure.record_start(store, {"source": "startup", "cwd": str(project), "session_id": "sample"}, NOW) == 1
    with store.transaction() as conn:
        row = conn.execute("SELECT exposed, detector FROM rule_exposures").fetchone()
    assert row["exposed"] == 1 and row["detector"] == miner.detector_version()
    monkeypatch.setattr(applier.publisher, "pr_details", lambda *_: {
        "state": "MERGED", "mergedAt": "2026-07-10T11:00:00Z",
    })
    expected = datetime(2026, 7, 10, 11, tzinfo=UTC)
    assert applier._merged(Repo(project), applier.git_row(store, "proposal"), "fake-url") == (True, expected, "github")


def test_exposure_uses_linked_worktree_instructions(tmp_path):
    store = SqliteStore(tmp_path / "wisen.db", "local")
    project, linked = tmp_path / "repo", tmp_path / "linked"
    head = _git_setup(project, "without rule")
    before = _git(project, "rev-parse", "HEAD:CLAUDE.md")
    _git(project, "worktree", "add", "--detach", str(linked), head)
    (linked / "CLAUDE.md").write_text("with rule")
    after = _git(linked, "hash-object", "CLAUDE.md")
    _proposal(store, project, head, before, after)
    for name, cwd in (("main", project), ("linked", linked)):
        assert exposure.record_start(store, {"source": "startup", "cwd": str(cwd), "session_id": name}, NOW) == 1
    with store.transaction() as conn:
        rows = conn.execute("SELECT session_id, exposed FROM rule_exposures").fetchall()
    assert dict(rows) == {"main": 0, "linked": 1}


def test_freeze_and_promotion_reject_standalone_agents_file(tmp_path):
    store = SqliteStore(tmp_path / "wisen.db", "local")
    _proposal(store, tmp_path, SHA, "a", "b", path="docs/AGENTS.md")
    frozen = manifest(case("target"))
    frozen.proposed = ReplayFiles(project={"docs/AGENTS.md": "new"}, blob="b")
    with pytest.raises(ValueError, match="does not load standalone AGENTS.md"):
        freeze_proposal(store, "proposal", [], model=frozen.model, harness=frozen.harness,
                        judge_model=frozen.judge_model)
    with pytest.raises(ValueError, match="does not load standalone AGENTS.md"):
        protocol.record_gate(store, frozen, {"manifest": frozen.digest(), "meets_rule": True})
    with pytest.raises(Exception) as local_freeze:  # a local model reads AGENTS.md: later checks fail instead
        freeze_proposal(store, "proposal", [], model="local:qwen", harness="local", judge_model=frozen.judge_model)
    assert "AGENTS.md" not in str(local_freeze.value)


def test_database_with_private_labels_is_owner_only(tmp_path):
    path = tmp_path / "wisen.db"
    store = SqliteStore(path, "local")
    assert path.stat().st_mode & 0o777 == 0o600
    with closing(store._connect()) as conn:
        conn.execute("SELECT * FROM reflections").fetchall()
        paths = [path, tmp_path / "wisen.db-wal", tmp_path / "wisen.db-shm"]
        for existing in paths:
            existing.chmod(0o644)
        SqliteStore(path, "local")
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in paths)


def test_blind_queue_agreement_and_failure_report(tmp_path):
    labels = calibration.Labels(SqliteStore(tmp_path / "wisen.db", "local"))
    frozen = manifest(case("target"), case("regression", "clean"))
    report = asyncio.run(runner.run_manifest(frozen, k=1, run=fake_run))
    assert calibration.import_replays(labels, frozen, report, "gold") == 4
    rows = labels.items("replay", "gold")
    queue = labels.blind("replay", "gold", "owner", 1)
    assert len(queue) == 4 and all("arm" not in item and "outcome" not in item for item in queue)
    for row in rows:
        verdict = row["slices"]["verdict"]
        labels.label(row["id"], "owner", 1, verdict, "rule wording" if verdict == "fail" else None)
    assert not labels.blind("replay", "gold", "owner", 1)
    version = calibration.judge_version(frozen.judge_model, frozen.judge_prompt, 3)
    agreements = labels.comparisons("replay", "gold", "owner", "judge", version)
    stats = calibration.calibration(agreements, "replay")
    assert stats["overall"]["kappa"] == 1
    assert stats["overall"]["confusion"]["pass"]["pass"] == 3
    assert stats["overall"]["per_class"]["abstain"]["precision"] is None
    assert stats["slices"]["arm"]["proposed"]["n"] == 2
    assert calibration.failure_report(labels, "gold", "owner")["categories"]["rule wording"]["count"] == 1


def test_observer_turns_and_stratified_negatives(tmp_path, monkeypatch):
    turns = [Turn(role="user", content="Add tests", timestamp=NOW),
             Turn(role="assistant", content="Done", timestamp=NOW + timedelta(minutes=1)),
             Turn(role="user", content="No, add an integration test", timestamp=NOW + timedelta(minutes=2))]
    session = NormalizedSession(session_id="fixture", source="claude-code", project_path="/demo", project_name="demo",
                                date=NOW.date().isoformat(), started_at=NOW, ended_at=turns[-1].timestamp, turns=turns,
                                metadata=compute_metadata(turns), raw_source_path="/local")
    key = agent.turn_id(session, 2)

    async def load(_):
        return IngestResult(sessions=[session])

    async def generate(_system, prompt, _schema, _model):
        assert f"[user-turn-id {key}]\nYou: No, add" in prompt
        return DayObservation(mood_signals_user="x", mood_signals_agent="x", topics_raw=[], key_moments=[],
                              commit_drift="", config_changes_observed=[], friction_observed=[],
                              yesterday_recs_followed=[], yesterday_recs_ignored=[], model_usage_observed=[],
                              day_summary="x", turn_corrections=[TurnCorrection(turn_id=key, kind="explicit-correction")])

    monkeypatch.setattr(agent, "_generate", generate)
    monkeypatch.setattr("wisen.ingestion.pipeline.ingest_all", load)
    labels = calibration.Labels(SqliteStore(tmp_path / "wisen.db", "local"))
    result = asyncio.run(calibration.sample_turns(labels, [session.date], "turns", n=2))
    assert result["items"] == 2 and result["heuristic_hits"] == 1
    for row in labels.blind("turn", "turns", "owner", 1):
        labels.label(row["id"], "owner", 1, "explicit-correction" if row["turn"].startswith("No") else "none")
    for scorer in ("miner", "observer"):
        rows = labels.comparisons("turn", "turns", "owner", scorer, result[f"{scorer}_version"])
        assert calibration.calibration(rows, "turn")["overall"]["kappa"] == 1
    observation = asyncio.run(agent.observe(asyncio.run(load(None)), session.date, None, "fake"))
    assert "turn_corrections" not in observation_packet(observation, session.date)


def test_normalized_miner_does_not_count_claude_tool_output_as_user_text():
    def event(content, minutes):
        return {"message": {"role": "user", "content": content},
                "timestamp": (NOW + timedelta(minutes=minutes)).isoformat()}

    turns = _events_to_turns([
        event([{"type": "tool_result", "content": "command output\nNo, ignore this"}], 0),
        event("Run the test suite", 0.5),
        event([{"type": "tool_result", "content": f"command output\n{miner.TOOL_REJECTION_MARKER}"}], 2),
        event([{"type": "tool_result", "content": "command output\nordinary output"},
               {"type": "text", "text": "No, use the integration suite"}], 3),
    ])
    session = NormalizedSession(session_id="tool-test", source="claude-code", project_path="/demo",
                                project_name="demo", date=NOW.date().isoformat(), started_at=NOW,
                                ended_at=turns[-1].timestamp, turns=turns, metadata=compute_metadata(turns),
                                raw_source_path="/local")
    assert list(miner.normalized_predictions(session)) == [
        (1, None), (2, "tool-rejection"), (3, "explicit-correction"),
    ]


def test_field_difference_in_differences_keeps_unknown_separate():
    def row(cohort, days, exposed, corrections):
        return {"cohort": cohort, "date": (NOW + timedelta(days=days)).date(),
                "exposed": exposed, "turns": 100, "corrections": corrections}

    rows = [row("treated", -2, False, 12), row("treated", 8, True, 5),
            row("control", -2, False, 15), row("control", 8, False, 12),
            row("unknown", 8, None, 1), row("mixed", -2, True, 4)]
    result = exposure.difference_in_differences(rows, NOW)
    assert result["difference_per_100"] == -4
    assert result["unknown_sessions"] == 1 and result["excluded_cohorts"] == 1
    assert exposure.difference_in_differences(rows[:2], NOW)["difference_per_100"] is None


def test_retention_replays_prior_rules_and_separates_holdout(tmp_path):
    store = SqliteStore(tmp_path / "wisen.db", "local")
    first = manifest(case("previous", project=tmp_path), case("unseen", "holdout", project=tmp_path),
                     proposal_id="first")
    latest = manifest(case("new-task"), proposal_id="latest")
    latest.proposed.project["CLAUDE.md"] = "overbroad replacement"
    _proposal(store, tmp_path, SHA, "a", "b", "first", at=NOW - timedelta(days=7))
    _proposal(store, tmp_path, SHA, "b", "c", "latest", at=NOW)
    results = asyncio.run(protocol.retention(store, [first], latest, k=1, run=fake_run))
    assert results[0]["rollback_recommended"]
    assert results[0]["holdout_transfer"]["cases"] == {"holdout": 1}


def test_retention_applies_all_merged_project_paths_and_home_changes(tmp_path):
    project = tmp_path / "project"
    store = SqliteStore(tmp_path / "wisen.db", "local")
    first = manifest(case("first-case", project=project), proposal_id="first")
    first.proposed.project["CLAUDE.md"] = "earliest project rule"
    home = manifest(case("home-case", project=project), proposal_id="home")
    home.proposed = ReplayFiles(config={"CLAUDE.md": "earlier home rule"})
    latest = manifest(case("latest-case", project=project), proposal_id="latest")
    latest.proposed = ReplayFiles(config={"CLAUDE.md": "stale snapshot", "rules/new.md": "latest rule"})
    _proposal(store, project, SHA, "a", "b", "first", at=NOW - timedelta(days=2))
    _proposal(store, tmp_path, SHA, "c", "d", "home", at=NOW - timedelta(days=1),
              path=".claude/CLAUDE.md", git_dir="home-git")
    _proposal(store, tmp_path, SHA, "e", "f", "latest", path=".claude/rules/new.md", git_dir="home-git")
    seen = []

    async def backend(item, variant, votes):
        if item.id == "home-case":
            seen.append((variant.config_files, variant.project_files))
        return {"passed": True}

    asyncio.run(protocol.retention(store, [home, first], latest, k=1, run=backend))
    assert ({"CLAUDE.md": "earlier home rule", "rules/new.md": "latest rule"},
            {"CLAUDE.md": "earliest project rule"}) in seen


def test_local_replay_sends_instructions_and_reads_reply(monkeypatch):
    sent = {}

    def post(url, payload):
        sent.update(url=url, payload=payload)
        return {"choices": [{"message": {"content": "uv add requests"}}], "usage": {"total_tokens": 42}}

    monkeypatch.setenv("WISEN_LOCAL_BASE_URL", "http://mac.local:9000/v1/")
    monkeypatch.setattr(replay, "_post_chat", post)
    item = case("local").case
    harness = replay.harness_version("local:qwen")
    result = replay.run_replay(item, config_files={"CLAUDE.md": "Use uv.", "rules/gone.md": None},
                               project_files={"CLAUDE.md": "Project rule.", "src/app.py": "code"},
                               model="local:qwen", harness=harness)
    assert (result.final_text, result.diff, result.tokens, result.error) == ("uv add requests", "", 42, "")
    assert sent["url"] == "http://mac.local:9000/v1/chat/completions"
    system, user = sent["payload"]["messages"]
    assert sent["payload"]["model"] == "qwen" and sent["payload"]["temperature"] > 0
    assert "Use uv." in system["content"] and "Project rule." in system["content"]
    assert "code" not in system["content"] and "gone" not in system["content"]
    assert user == {"role": "user", "content": replay.build_prompt(item)}
    assert "does not match" in replay.run_replay(item, model="local:qwen", harness="1.0").error
    monkeypatch.setattr(replay, "_post_chat", lambda *_: {"choices": []})
    assert replay.run_replay(item, model="local:qwen").error == "local model returned no message"

    def down(*_):
        raise replay.httpx.ConnectError("refused")
    monkeypatch.setattr(replay, "_post_chat", down)
    assert "request to http://mac.local:9000/v1/chat/completions failed" in replay.run_replay(item, model="local:qwen").error


def test_local_replay_reads_committed_project_instructions_and_skills(tmp_path, monkeypatch):
    project = tmp_path / "project"
    repo = Repo(project)
    project.mkdir()
    repo.run("init", "-q")
    (project / "CLAUDE.md").write_text("Committed rule.")
    (project / ".claude" / "rules").mkdir(parents=True)
    (project / "shared.md").write_text("Shared rule.")
    (project / ".claude" / "rules" / "shared.md").symlink_to("../../shared.md")
    (project / "src").mkdir()
    (project / "src" / "app.md").write_text("Not instructions.")
    (project / ".claude" / "commands").mkdir()
    (project / ".claude" / "commands" / "ship.md").write_text("Command prompt.")
    (project / "AGENTS.md").symlink_to("CLAUDE.md")
    (project / ".claude" / "skills" / "old").mkdir(parents=True)
    (project / ".claude" / "skills" / "old" / "notes.md").write_bytes("caf\xe9".encode("cp1252"))
    repo.run("add", ".")
    repo.run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    sent = {}

    def post(url, payload):
        sent.update(payload)
        return {"choices": [{"message": {"content": "ok"}}]}
    monkeypatch.setattr(replay, "_post_chat", post)
    item = case("local", project=project).case
    replay.run_replay(item, project_files={".claude/skills/t/SKILL.md": "Skill text.", "CLAUDE.md": "New rule.",
                                           ".claude/skills/t/reference.md": "Reference text."},
                      base_commit=repo.resolve("HEAD"), model="local:qwen")
    system = sent["messages"][0]["content"]
    assert all(text in system for text in ("New rule.", "Skill text.", "Reference text.", "Shared rule."))
    assert not any(text in system for text in ("Committed rule.", "Not instructions.", "Command prompt."))


def test_demo_bank_and_simulated_run_are_reproducible():
    from wisen.evals import demo

    bank = demo.load_bank()
    assert len({c.id for _, _, c in bank}) == len(bank)
    assert {cohort for cohort, _, _ in bank} == {"targeted", "clean", "holdout"}
    assert all(c.behavior_check.endswith("?") for _, _, c in bank)
    first, second = (asyncio.run(demo.run_demo()) for _ in range(2))
    assert first["paired"] == second["paired"] and first["simulated"]
    assert first["cases"] == {"eval": 12, "holdout": 6, "clean": 8}
    assert first["replays"] == len(bank) * 2 * 3


def test_demo_replays_local_and_claude_models(tmp_path):
    from wisen.evals import demo

    seen = []

    async def run(item, variant, votes):
        seen.append((item.project_path, variant.model, variant.base_commit, bool(variant.config_files)))
        if variant.base_commit:
            assert (Path(item.project_path) / ".git").exists()
        return {"passed": bool(variant.config_files) or item.kind == "clean"}

    report = asyncio.run(demo.run_demo("local:qwen", repeats=1, run=run))
    assert {(p, m, b) for p, m, b, _ in seen} == {("synthetic", "local:qwen", None)}
    assert report["meets_rule"] is True and not report["simulated"]
    seen.clear()
    asyncio.run(demo.run_demo("claude-sonnet-4-6", repeats=1, run=run))
    assert all(b and len(b) == 40 and p != "synthetic" for p, _, b, _ in seen)


def test_scorecard_case_rates_use_matched_pairs():
    from wisen.evals import scorecard

    outcomes = iter([{"passed": True}, {"error": "x"}, {"passed": True}, {"passed": False}])

    async def run(item, variant, votes):
        return next(outcomes)
    report = asyncio.run(runner._paired({"eval": [(case("a").case, "cur", "new")]}, 2, 1, 0.05, run, capture=True))
    result = report["case_results"][0]
    assert result["difference"] == 1 and result["matched_pass_rates"] == {"current": 0.0, "proposed": 1.0}
    assert "0% → 100%" in scorecard.render(report)
    result["attempts"][0]["current"]["outcome"] = "fail"  # its pair errored, so it is not in the matched rates
    for attempt in result["attempts"]:
        for arm in attempt.values():
            arm["evidence"] = "unmatched" if attempt["proposed"]["outcome"] == "error" else "matched"
    assert "matched" in scorecard.render(report, evidence=True)
    assert "unmatched" not in scorecard.render(report, evidence=True)


def test_scorecard_states_the_verdict_without_private_text():
    from wisen.evals import demo, scorecard

    frozen = manifest(case("regression", "clean"), case("target"))
    report = asyncio.run(runner.run_manifest(frozen, run=fake_run))
    text = scorecard.render(report)
    assert "**Verdict: Promote**" in text and "| Targeted (gate) | 1 | 0% | 100% | +100.0 pp |" in text
    simulated = asyncio.run(demo.run_demo())
    for attempt in simulated["case_results"][0]["attempts"]:
        attempt["proposed"]["evidence"] = attempt["current"]["evidence"] = "private quote"
    assert "Simulated" in scorecard.render(simulated) and "private quote" not in scorecard.render(simulated)
    simulated["case_results"][0]["attempts"][0]["current"] = {"outcome": "error", "error": "/home/me/app is not a git repo"}
    assert "/home/me/app" not in scorecard.render(simulated)
    assert "/home/me/app" in scorecard.render(simulated, evidence=True)
    report["meets_rule"] = None
    report["paired"]["clean"]["ci95"] = None
    assert "**Verdict: Insufficient evidence**" in scorecard.render(report)
