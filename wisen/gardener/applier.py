"""
The applier: turns an approved proposal into exactly one commit on one
branch, `wisen/gardener/<proposal id>`, in the target's repo. It runs on the
laptop, the only machine that can see the repo.

Before it writes anything it re-checks, from local records only: the proposal
is approved and the decision record says so; the target passes the
allow-list (wisen/gardener/targets.py); the diff touches that one file and
nothing else; and the file is unchanged since the diff was made. A refusal
moves the proposal to `failed` with the reason; nothing is written.

Exactly once, even across a crash or a reconnect: the base commit is
journaled before the commit is made, and the commit's identity, time
(the approval's) and message are fixed, so a rerun builds the same commit id;
the branch is created only if it does not exist (or already points at that
commit). The commit is built in a temporary index, so no checkout, index or
work tree of yours is touched.

Merge detection (`detect_merges`) watches applied branches: in the default
branch (merged, also by squash: the file there matches the branch), or
deleted unmerged (abandoned). A merge starts the 2-week measure timer.
"""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from wisen.gardener.git import GitError, Repo, diff_paths
from wisen.gardener.targets import Refused, Target, resolve
from wisen.jobs import insert_job, ts
from wisen.proposals import Proposals
from wisen.store import SqliteStore

MEASURE_AFTER = timedelta(days=14)


def branch_name(proposal_id: str) -> str:
    return f"wisen/gardener/{proposal_id}"


def record_found(t: Target, when: str) -> None:
    """Home only: make main hold the file as it is on disk, so a branch from
    main changes exactly what the proposal shows, and keep the home repo's
    own index in step with main, so your merge sees a clean tree."""
    repo, main = t.repo, t.repo.resolve("refs/heads/main")
    live = t.live_file()
    blob = repo.out("hash-object", "-w", "--", str(live)) if live.is_file() else None
    if repo.blob(main, t.path) == blob:
        return
    with repo.scratch_index() as env:
        repo.run("read-tree", main, env=env)
        if blob:
            repo.run("update-index", "--add", "--cacheinfo", f"100644,{blob},{t.path}", env=env)
        else:
            repo.run("update-index", "--force-remove", "--", t.path, env=env)
        tree = repo.out("write-tree", env=env)
    commit = repo.commit_tree(tree, main, f"wisen: record {t.path} as found on disk\n", when)
    repo.run("update-ref", "refs/heads/main", commit, main)
    repo.run("read-tree", "refs/heads/main")


def check(store: SqliteStore, proposal_id: str) -> dict:
    """The re-checks; returns what the commit step needs, or {"refused": why}."""
    with closing(store._connect()) as conn:
        row = conn.execute("SELECT * FROM proposals WHERE user_id = ? AND id = ?",
                           (store.user_id, proposal_id)).fetchone()
        memo = conn.execute("SELECT * FROM proposal_decisions WHERE user_id = ? AND proposal_id = ?",
                            (store.user_id, proposal_id)).fetchone()
    if row is None or row["state"] != "approved" or memo is None or memo["decision"] != "approved":
        return {"refused": "no approval on record"}
    payload = json.loads(row["payload"])
    try:
        t = resolve(row["target"])
        # git's own date format; the approval's time, so a rerun commits the same.
        when = f"{int(datetime.fromisoformat(memo['at']).timestamp())} +0000"
        if t.is_home:
            record_found(t, when)
        base = t.base()
        if base is None:
            raise Refused(f"{t.alias} has no default branch to start from")
        if diff_paths(t.repo, payload["diff"]) != [t.path]:
            raise Refused(f"the diff changes files other than {t.path}")
        if t.repo.blob(base, t.path) != payload.get("base_blob"):
            raise Refused(f"{t.name} changed since the diff was made")
    except (Refused, GitError) as e:
        return {"refused": str(e)}
    return {"work_tree": str(t.repo.work_tree), "git_dir": str(t.repo.git_dir) if t.repo.git_dir else None,
            "path": t.path, "base": base, "when": when, "branch": branch_name(proposal_id)}


def _message(proposal_id: str, target: str, rationale: str) -> str:
    first = " ".join(rationale.split())[:200]
    return f"wisen gardener: update {target}\n\n{first}\n\nProposal: {proposal_id}\n"


def commit(info: dict, proposal_id: str, target: str, rationale: str, diff: str) -> dict:
    """Make the branch's one commit (or find it, on a rerun). Raises Refused
    when the diff does not apply or the branch already holds something else."""
    repo = Repo(Path(info["work_tree"]), Path(info["git_dir"]) if info["git_dir"] else None)
    with repo.scratch_index() as env:
        repo.run("read-tree", info["base"], env=env)
        if not repo.ok("apply", "--cached", "--whitespace=nowarn", "-", env=env, input=diff):
            raise Refused(f"the diff no longer applies to {target}")
        tree = repo.out("write-tree", env=env)
    sha = repo.commit_tree(tree, info["base"], _message(proposal_id, target, rationale), info["when"])
    if not repo.create_ref(f"refs/heads/{info['branch']}", sha):
        raise Refused(f"branch {info['branch']} already exists with other content")
    return {"commit": sha, "new_blob": repo.blob(sha, info["path"])}


def record_applied(store: SqliteStore, proposals: Proposals, proposal_id: str, info: dict, made: dict) -> None:
    now = ts(proposals.clock())
    with store.transaction() as conn:
        proposals.transition_in(conn, proposal_id, "approved", "applied", f"applied:{proposal_id}", "applier",
                                info["branch"])
        conn.execute(
            "INSERT INTO proposal_git (proposal_id, user_id, git_dir, work_tree, path, branch, base_commit, "
            "commit_sha, new_blob, applied_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (proposal_id) DO NOTHING",
            (proposal_id, store.user_id, info["git_dir"] or "", info["work_tree"], info["path"], info["branch"],
             info["base"], made["commit"], made["new_blob"], now),
        )


def record_failed(proposals: Proposals, proposal_id: str, reason: str) -> None:
    with proposals.store.transaction() as conn:
        proposals.transition_in(conn, proposal_id, "approved", "failed", f"failed:{proposal_id}", "applier",
                                reason[:1000])


# ── merge detection ───────────────────────────────────────────────────────────

def git_row(store: SqliteStore, proposal_id: str) -> sqlite3.Row | None:
    with closing(store._connect()) as conn:
        return conn.execute("SELECT * FROM proposal_git WHERE user_id = ? AND proposal_id = ?",
                            (store.user_id, proposal_id)).fetchone()


def _merged(repo: Repo, row: sqlite3.Row) -> bool | None:
    """True if merged; False if the branch is gone unmerged; None if still open."""
    refs = ["refs/heads/main"] if row["git_dir"] else repo.default_refs()
    for ref in refs:
        if repo.ok("merge-base", "--is-ancestor", row["commit_sha"], ref):
            return True
        if row["new_blob"] and repo.blob(ref, row["path"]) == row["new_blob"]:
            return True  # squash or rebase merge: the file on the default branch is the branch's
    return None if repo.resolve(f"refs/heads/{row['branch']}") else False


def detect_merges(store: SqliteStore, proposals: Proposals) -> list[tuple[str, str]]:
    """Move applied proposals whose branch merged (or was deleted unmerged).
    A merge sets the measure timer in the same transaction. Safe to call
    any time: each move has a fixed request id."""
    moved = []
    with closing(store._connect()) as conn:
        applied = [r["id"] for r in conn.execute(
            "SELECT id FROM proposals WHERE user_id = ? AND state = 'applied'", (store.user_id,))]
    for proposal_id in applied:
        row = git_row(store, proposal_id)
        if row is None:
            continue
        repo = Repo(Path(row["work_tree"]), Path(row["git_dir"]) if row["git_dir"] else None)
        try:
            merged = _merged(repo, row)
        except GitError as e:
            print(f"[gardener] cannot check {row['branch']}: {e}")
            continue
        if merged is None:
            continue
        now = proposals.clock()
        with store.transaction() as conn:
            if merged:
                proposals.transition_in(conn, proposal_id, "applied", "merged", f"merged:{proposal_id}", "applier",
                                        row["branch"])
                conn.execute("UPDATE proposal_git SET merged_at = COALESCE(merged_at, ?) WHERE proposal_id = ?",
                             (ts(now), proposal_id))
                insert_job(conn, store.user_id, "measure-proposal", {"proposal_id": proposal_id},
                           request_id=f"measure:{proposal_id}", run_after=now + MEASURE_AFTER, max_attempts=3,
                           now=now)
            else:
                proposals.transition_in(conn, proposal_id, "applied", "abandoned", f"abandoned:{proposal_id}",
                                        "applier", f"{row['branch']} was deleted unmerged")
        moved.append((proposal_id, "merged" if merged else "abandoned"))
    return moved


def merged_at(store: SqliteStore, proposal_id: str) -> datetime:
    return datetime.fromisoformat(git_row(store, proposal_id)["merged_at"])
