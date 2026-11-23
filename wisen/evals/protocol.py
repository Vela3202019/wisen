"""Persist promotion evidence and check earlier rules against cumulative config."""
import json
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from wisen.evals.manifest import (
    Manifest,
    ReplayFiles,
    _private_open,
    require_claude_target,
)
from wisen.evals.runner import RunCase, run_manifest
from wisen.gardener.applier import git_row
from wisen.store import SqliteStore


def write_report(path: Path, report: dict) -> None:
    with open(path, "x", encoding="utf-8", opener=_private_open) as output:
        json.dump(report, output, indent=2)


def summary(report: dict) -> dict:
    return {key: value for key, value in report.items() if key != "case_results"}


def record_gate(store: SqliteStore, manifest: Manifest, report: dict) -> dict:
    applied = git_row(store, manifest.proposal_id)
    if report.get("holdout") or report.get("manifest") != manifest.digest() or applied is None:
        raise ValueError("the gate requires a non-holdout report for an applied proposal")
    require_claude_target(applied["path"], home=bool(applied["git_dir"]))
    if manifest.proposed.blob != applied["new_blob"] or any(entry.cumulative_project_files for entry in manifest.cases):
        raise ValueError("the gate requires the proposal's frozen Git variant, not a cumulative check")
    meets = report["meets_rule"]
    decision = "promote" if meets is True else "hold" if meets is False else "insufficient_evidence"
    at = datetime.now(UTC).isoformat()
    gate = {"decision": decision, "at": at, **summary(report)}
    with store.transaction() as conn:
        conn.execute("""
            INSERT INTO proposal_measures (proposal_id, user_id, verdict, reason, result, at, replay_gate)
            VALUES (?, ?, 'pending', '', '{}', ?, ?)
            ON CONFLICT (proposal_id) DO UPDATE SET replay_gate=excluded.replay_gate
            WHERE proposal_measures.user_id=excluded.user_id
        """, (manifest.proposal_id, store.user_id, at, json.dumps(gate)))
    return gate


def evidence(store: SqliteStore, proposal_id: str) -> dict:
    with closing(store._connect()) as conn:
        row = conn.execute("SELECT verdict, result, replay_gate FROM proposal_measures WHERE user_id=? AND proposal_id=?",
                           (store.user_id, proposal_id)).fetchone()
    if row is None:
        return {"replay_gate": None, "observational_field": None}
    return {"replay_gate": json.loads(row["replay_gate"]) if row["replay_gate"] else None,
            "observational_field": json.loads(row["result"]) if row["verdict"] != "pending" else None}


async def retention(store: SqliteStore, earlier: list[Manifest], cumulative: Manifest, *,
                    k: int = 5, votes: int = 3, margin: float = 0.05, run: RunCase | None = None) -> list[dict]:
    latest = git_row(store, cumulative.proposal_id)
    if latest is None or latest["merged_at"] is None:
        raise ValueError("the cumulative manifest's proposal must be merged")
    seen = set()
    ordered = []
    for old in earlier:
        row = git_row(store, old.proposal_id)
        if (row is None or row["merged_at"] is None or old.proposal_id in seen
                or datetime.fromisoformat(row["merged_at"]) >= datetime.fromisoformat(latest["merged_at"])):
            raise ValueError("retention requires unique earlier, merged proposals")
        seen.add(old.proposal_id)
        ordered.append((old, row))
    ordered.sort(key=lambda entry: datetime.fromisoformat(entry[1]["merged_at"]))
    config = dict(cumulative.proposed.config)
    project_changes = []
    for manifest, row in [*ordered, (cumulative, latest)]:
        path = row["path"]
        if row["git_dir"]:
            rel = path.removeprefix(".claude/")
            config[rel] = manifest.proposed.config[rel]
        else:
            project_changes.append((Path(row["work_tree"]).resolve(), path, manifest.proposed.project[path]))
    accumulated = ReplayFiles(config=config)
    reports = []
    for old, _ in ordered:
        cases = []
        for entry in old.cases:
            project = Path(entry.case.project_path).resolve()
            overlays = dict(entry.cumulative_project_files)
            for root, path, text in project_changes:
                if project.is_relative_to(root):
                    overlays[path] = text
            cases.append(entry.model_copy(update={"cumulative_project_files": overlays}))
        kwargs = {"model": cumulative.model, "harness": cumulative.harness, "judge_model": cumulative.judge_model,
                  "judge_prompt": cumulative.judge_prompt, "proposed": accumulated}
        targeted = [entry for entry in cases if entry.cohort == "targeted"]
        holdout = [entry for entry in cases if entry.cohort == "holdout"]
        check = old.model_copy(update={**kwargs, "current": old.proposed, "cases": targeted})
        result = await run_manifest(check, k=k, judge_votes=votes, clean_margin=margin, run=run)
        metric = result["paired"].get("eval", {})
        ci = metric.get("ci95")
        rollback = (bool(ci and ci[1] < -margin)
                    if ci and all(not c["error"] and not c["abstain"] for c in metric["outcomes"].values()) else None)
        item = {"earlier_proposal": old.proposal_id, "earlier_manifest": old.digest(),
                "cumulative_manifest": cumulative.digest(), "retention": summary(result),
                "rollback_recommended": rollback}
        if holdout:
            check = old.model_copy(update={**kwargs, "current": old.current, "cases": holdout})
            result = await run_manifest(check, k=k, judge_votes=votes, clean_margin=margin, holdout=True, run=run)
            item["holdout_transfer"] = summary(result)
        reports.append(item)
    return reports
