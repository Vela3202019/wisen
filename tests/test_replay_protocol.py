"""Offline tests of the frozen protocol; no real agent or judge runs."""
import asyncio
import json
import os
import subprocess
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
