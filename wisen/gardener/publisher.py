"""
Publishing an applied proposal: push its branch, `wisen/gardener/<id>`, to
the repo's remote and open a pull request against the default branch with the
`gh` CLI. It runs on the laptop, as a device job that `wisen worker` claims
when the owner taps "Push & create PR" in the dashboard; the server cannot
reach GitHub and holds no token for it.

Before anything leaves the machine it re-checks, from local records only, as
the applier does: the approval record exists, the proposal is applied, and
the branch still points at the commit the applier made. Only that branch is
pushed, with an explicit refspec and never with force; the default branch is
never pushed. A rerun (a second tap, a retry after a lost lease) finds the
branch's open pull request and returns it instead of opening another.

`home` targets (files outside any repo) have no remote: they stay a local
branch the owner merges. The dashboard hides the button for them, and this
refuses them.

A failure is reported to the server by class name only (NotApplied, NoRemote,
NoGhAuth, PushFailed, PrFailed); the message, which may name paths, stays in
the laptop's log.
"""
import json
import os
import subprocess
from contextlib import closing
from pathlib import Path

from wisen.gardener.applier import git_row
from wisen.gardener.git import GitError, Repo
from wisen.gardener.targets import HOME
from wisen.proposals import Proposals
from wisen.store import SqliteStore


class PublishError(Exception):
    """Nothing was published (or the pull request is not known); the class says why."""


class NotApplied(PublishError):
    """No approval on record, the proposal is not applied, or the branch moved."""


class NoRemote(PublishError):
    """The repo has no git remote to push to (every `home` target)."""


class NoGhAuth(PublishError):
    """The gh CLI is missing or not logged in."""


class PushFailed(PublishError):
    pass


class PrFailed(PublishError):
    pass


def check(store: SqliteStore, proposal_id: str) -> dict:
    """The re-checks; returns the repo, branch and proposal content to publish."""
    with closing(store._connect()) as conn:
        row = conn.execute("SELECT * FROM proposals WHERE user_id = ? AND id = ?",
                           (store.user_id, proposal_id)).fetchone()
        memo = conn.execute("SELECT decision FROM proposal_decisions WHERE user_id = ? AND proposal_id = ?",
                            (store.user_id, proposal_id)).fetchone()
    if row is None or memo is None or memo["decision"] != "approved":
        raise NotApplied("no approval on record")
    if row["state"] != "applied":
        raise NotApplied(f"proposal {proposal_id} is {row['state']}, not applied")
    if row["target"].partition(":")[0] == HOME:
        raise NoRemote(f"{row['target']} is a file outside any repo: its branch has no remote to go to")
    git = git_row(store, proposal_id)
    if git is None:
        raise NotApplied("no branch on record")
    repo = Repo(Path(git["work_tree"]), Path(git["git_dir"]) if git["git_dir"] else None)
    if repo.resolve(f"refs/heads/{git['branch']}") != git["commit_sha"]:
        raise NotApplied(f"{git['branch']} is not at the commit the applier made")
    return {"repo": repo, "branch": git["branch"], "target": row["target"], "payload": json.loads(row["payload"])}


def _remote(repo: Repo) -> str:
    remotes = repo.out("remote").split()
    if not remotes:
        raise NoRemote(f"{repo.work_tree} has no git remote")
    return "origin" if "origin" in remotes else remotes[0]


def _gh(work_tree: Path, *args: str, input: str | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "GIT_TERMINAL_PROMPT": "0"}
    try:
        return subprocess.run(["gh", *args], cwd=work_tree, env=env, capture_output=True, text=True, input=input,
                              check=False)
    except FileNotFoundError:
        raise NoGhAuth("the gh CLI is not installed") from None


def _check_auth(work_tree: Path) -> None:
    r = _gh(work_tree, "auth", "status")
    if r.returncode != 0:
        raise NoGhAuth(f"gh is not logged in: {r.stderr.strip()[:300]}")


def open_pr(work_tree: Path, branch: str) -> str | None:
    """The URL of the branch's open pull request, if it has one."""
    r = _gh(work_tree, "pr", "list", "--head", branch, "--state", "open", "--json", "url")
    if r.returncode != 0:
        raise PrFailed(f"gh pr list failed: {r.stderr.strip()[:300]}")
    try:
        prs = json.loads(r.stdout or "[]")
    except ValueError:
        raise PrFailed("gh pr list printed something other than JSON") from None
    return prs[0]["url"] if prs else None


def title(target: str) -> str:
    return f"wisen gardener: update {target}"


def body(proposal_id: str, branch: str, payload: dict) -> str:
    lines = [" ".join(payload.get("rationale", "").split()), ""]
    if payload.get("evidence"):
        lines += ["Evidence:", *(f"- \"{q}\"" for q in payload["evidence"]), ""]
    if payload.get("eval_summary"):
        lines += [payload["eval_summary"], ""]
    lines.append(f"Proposal `{proposal_id}`, opened by wisen from branch `{branch}`. Merging is yours.")
    return "\n".join(lines) + "\n"


def create_pr(work_tree: Path, branch: str, base: str, title: str, body: str) -> str:
    r = _gh(work_tree, "pr", "create", "--head", branch, "--base", base, "--title", title, "--body-file", "-",
            input=body)
    url = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    if r.returncode != 0 or not url.startswith("https://"):
        raise PrFailed(f"gh pr create failed: {(r.stderr or r.stdout).strip()[:300]}")
    return url


def publish(store: SqliteStore, proposal_id: str) -> str:
    """Push the applied branch and open its pull request, or find the one
    already open for it. Returns the pull request's URL, recorded on the
    proposal."""
    info = check(store, proposal_id)
    repo, branch = info["repo"], info["branch"]
    remote = _remote(repo)
    _check_auth(repo.work_tree)
    url = open_pr(repo.work_tree, branch)
    if url is None:
        try:
            repo.run("push", remote, f"refs/heads/{branch}:refs/heads/{branch}")
        except GitError as e:
            raise PushFailed(str(e)) from None
        url = create_pr(repo.work_tree, branch, repo.default_branch(), title(info["target"]),
                        body(proposal_id, branch, info["payload"]))
    with store.transaction() as conn:
        Proposals(store).record_pr_in(conn, proposal_id, url)
    return url
