"""Observe the rule file present at Claude Code startup; never infer historical exposure."""
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from wisen.evals.miner import detector_version, scan_all
from wisen.evals.verification import FIELD_DAYS, ROLLING_DAYS
from wisen.gardener.git import GitError, Repo
from wisen.store import SqliteStore


def record_start(store: SqliteStore, payload: dict, at: datetime | None = None) -> int:
    if payload.get("source") != "startup" or not payload.get("session_id") or not payload.get("cwd"):
        return 0
    cwd = Path(payload["cwd"]).resolve()
    repo = Repo(cwd)
    try:
        root = repo.out("rev-parse", "--show-toplevel")
        branch = repo.run("symbolic-ref", "--quiet", "HEAD", check=False).stdout.strip()
        common_dir = Path(repo.out("rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    except GitError:
        root, branch, common_dir = str(cwd), "", None
    cohort = sha256(f"{root}:{branch}".encode()).hexdigest()[:24]
    now = (at or datetime.now(UTC)).isoformat()
    with closing(store._connect()) as conn:
        proposals = conn.execute("""
            SELECT g.*, json_extract(p.payload, '$.base_blob') AS old_blob
            FROM proposal_git g JOIN proposals p ON g.proposal_id=p.id
            WHERE g.user_id=? AND p.state IN ('applied','merged','measuring','kept','revert_proposed')
        """, (store.user_id,)).fetchall()

    rows = []
    for proposal in proposals:
        exposed = _rule_exposure(proposal, cwd, root, common_dir)
        rows.append((store.user_id, proposal["proposal_id"], payload["session_id"], cohort, now,
                     exposed, detector_version()))
    with store.transaction() as conn:
        conn.executemany("INSERT OR IGNORE INTO rule_exposures VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def _rule_exposure(proposal, cwd: Path, root: str, common_dir: Path | None) -> bool | None:
    directory = Path(proposal["work_tree"]).resolve()
    git_dir = Path(proposal["git_dir"]) if proposal["git_dir"] else None
    if proposal["path"] not in ("CLAUDE.md", ".claude/CLAUDE.md"):
        return None
    try:
        if git_dir is None and not cwd.is_relative_to(directory):
            if common_dir is None:
                return False
            proposal_common = Repo(directory).out("rev-parse", "--path-format=absolute", "--git-common-dir")
            if Path(proposal_common).resolve() != common_dir:
                return False
            directory = Path(root).resolve()
        path = directory / proposal["path"]
        if path.is_symlink() or not path.resolve().is_relative_to(directory):
            return None
        repo = Repo(directory, git_dir)
        blob = repo.out("hash-object", "--no-filters", "--", str(path)) if path.exists() else None
    except (GitError, OSError):
        return None
    old, new = proposal["old_blob"], proposal["new_blob"]
    # A later edit may retain the rule; a different blob does not prove non-exposure.
    return True if blob == new and new != old else False if blob == old else None


def difference_in_differences(rows: list[dict], merged_at: datetime) -> dict:
    day = merged_at.astimezone(UTC).date()
    before = day - timedelta(days=ROLLING_DAYS)
    after = day + timedelta(days=FIELD_DAYS - ROLLING_DAYS)
    end = day + timedelta(days=FIELD_DAYS)
    cohorts: dict[str, dict] = defaultdict(lambda: {"before": [], "after": []})
    unknown = 0
    for row in rows:
        date = row["date"]
        period = "before" if before <= date < day else "after" if after <= date < end else None
        if period is None:
            continue
        if row["exposed"] is None:
            unknown += 1
            continue
        cohorts[row["cohort"]][period].append(row)
    groups: dict = {kind: {period: [] for period in ("before", "after")} for kind in ("treated", "control")}
    excluded = 0
    for cohort in cohorts.values():
        pre, post = cohort["before"], cohort["after"]
        if not pre or not post or any(row["exposed"] for row in pre):
            excluded += 1
            continue
        exposures = {row["exposed"] for row in post}
        if len(exposures) != 1:
            excluded += 1
            continue
        group = "treated" if exposures.pop() else "control"
        groups[group]["before"].extend(pre)
        groups[group]["after"].extend(post)

    def summarize(records):
        turns = sum(row["turns"] for row in records)
        corrections = sum(row["corrections"] for row in records)
        return {"sessions": len(records), "turns": turns, "corrections": corrections,
                "corrections_per_100": 100 * corrections / turns if turns else None}

    cells = {kind: {period: summarize(records) for period, records in windows.items()}
             for kind, windows in groups.items()}
    t0, t1, c0, c1 = (cells[kind][period]["corrections_per_100"] for kind in ("treated", "control")
                        for period in ("before", "after"))
    return {"observational": True, "cells": cells, "unknown_sessions": unknown, "excluded_cohorts": excluded,
            "difference_per_100": (t1 - t0) - (c1 - c0) if None not in (t0, t1, c0, c1) else None}


def field_evidence(store: SqliteStore, proposal_id: str, claude_dir: Path, merged_at: datetime) -> dict:
    version = detector_version()
    with closing(store._connect()) as conn:
        rows = conn.execute("SELECT * FROM rule_exposures WHERE user_id=? AND proposal_id=?",
                            (store.user_id, proposal_id)).fetchall()
    exposures = {row["session_id"]: row for row in rows if row["detector"] == version}
    records = []
    if exposures:
        start = merged_at.astimezone(UTC).date() - timedelta(days=ROLLING_DAYS)
        for session in scan_all(claude_dir, start.isoformat()):
            if session.session_id in exposures:
                row = exposures[session.session_id]
                records.append({"date": datetime.fromisoformat(row["observed_at"]).astimezone(UTC).date(),
                                "cohort": row["cohort"], "exposed": row["exposed"], "turns": session.user_turns,
                                "corrections": len(session.cases)})
    return {**difference_in_differences(records, merged_at), "detector_version": version,
            "recorded_sessions": len(exposures), "matched_sessions": len(records),
            "other_detector_sessions": len(rows) - len(exposures)}
