"""
The verification loop's numbers: is wisen climbing the hill?
(docs/plan/cloud-architecture.md §5.4). Deterministic: transcripts, local git
and reflections only, no model.

  daily metrics   per day: user turns, corrections per 100 user turns by
                  detector, tokens (and $ where the transcript records it),
                  problems solved, tokens / turns / $ per problem solved,
                  human-in-the-loop turns per PR, and the quality guard
                  (solved problems later reverted or re-fixed)
  field check     two weeks after a Gardener change merged: 7-day rolling
                  medians before vs after, and a verdict (helped / no change /
                  worse) with its reason

"Problems solved" is the owner's confirmed definition; it lives in
`problems_solved` alone, so changing it changes nothing else.
"""
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from wisen import config
from wisen.evals.metrics import friction_half_life
from wisen.evals.miner import SessionScan, scan_all
from wisen.gardener.git import GitError, Repo

ROLLING_DAYS = 7          # the rolling-median window
FIELD_DAYS = 14           # measure this long after merge
FLAT = 0.05               # a change within ±5% is flat
TOKENS_WORSE = 0.10       # tokens per problem solved up by more than this is worse
LAND_GRACE = timedelta(hours=1)   # a commit this soon after a session ends still counts for it
REFIX_DAYS = 7
SESSION_LOOKBACK = timedelta(days=30)  # how far back sessions of a branch are looked for
DETECTORS = ("tool_rejection", "interruption", "explicit_correction", "rephrase_storm")  # miner names, as keys

_MERGE_PR = re.compile(r"^Merge pull request #\d+ from [^/\s]+/(\S+)")
_MERGE_BRANCH = re.compile(r"^Merge (?:remote-tracking )?branch '(?:origin/)?([^']+)'")
_REVERTS = re.compile(r"This reverts commit ([0-9a-f]{7,40})")
_FIX = re.compile(r"^fix\b", re.IGNORECASE)


@dataclass
class Commit:
    sha: str
    at: datetime
    author: str           # the author's email, lowercased
    parents: list[str]
    subject: str
    body: str
    files: set[str]


@dataclass
class Solved:
    """One solved problem: a branch that landed (one PR), or a session on the
    default branch that ended in a landed commit."""
    repo: str
    branch: str | None
    landed_at: datetime
    commits: list[str]
    files: set[str]
    user_turns: int
    reverted: bool = False
    refixed: bool = False


@dataclass
class _RepoHistory:
    repo: Repo
    default: str
    refs: list[str]
    commits: list[Commit] = field(default_factory=list)   # first-parent, oldest first


def _toplevel(path: str, cache: dict) -> str | None:
    if path not in cache:
        cache[path] = None
        if Path(path).is_dir():
            try:
                cache[path] = Repo(work_tree=Path(path)).out("rev-parse", "--show-toplevel") or None
            except GitError:
                pass
    return cache[path]


def _history(root: str, since: datetime) -> _RepoHistory | None:
    repo = Repo(work_tree=Path(root))
    try:
        refs = repo.default_refs()
        if not refs:
            return None
        out = repo.out("log", "--first-parent", "--name-only", f"--since={since.isoformat()}",
                       "--format=%x1e%H%x1f%cI%x1f%ae%x1f%P%x1f%s%x1f%b%x1f", *refs)
        default = repo.default_branch()
    except GitError:
        return None
    commits = []
    for record in out.split("\x1e"):
        if "\x1f" not in record:
            continue
        sha, at, author, parents, subject, body, files = record.split("\x1f", 6)
        commits.append(Commit(sha.strip(), datetime.fromisoformat(at).astimezone(UTC), author.lower(),
                              parents.split(), subject, body, {f for f in files.splitlines() if f.strip()}))
    commits.sort(key=lambda c: c.at)
    return _RepoHistory(repo, default, refs, commits)


def _owner(repo: Repo) -> str:
    """The owner's git email: `[gardener].owner_email`, else the repo's `user.email`."""
    return (config.get("gardener", "owner_email", "WISEN_OWNER_EMAIL")
            or repo.run("config", "user.email", check=False).stdout.strip()).lower()


def _merged_range(h: _RepoHistory, merge: Commit) -> tuple[list[str], set[str], set[str]]:
    """The commits a merge brought in (the merge first), the files it
    changed, and their authors."""
    try:
        log = h.repo.out("log", "--format=%H %ae", f"{merge.parents[0]}..{merge.parents[1]}").splitlines()
        files = set(h.repo.out("diff", "--name-only", merge.parents[0], merge.sha).splitlines())
    except GitError:
        log, files = [], set()
    pairs = [line.split(" ", 1) for line in log if " " in line]
    return [merge.sha, *(sha for sha, _ in pairs)], files, {merge.author, *(a.lower() for _, a in pairs)}


def problems_solved(sessions: list[SessionScan], since: datetime) -> list[Solved]:
    """THE DEFINITION (confirmed by the owner; §5.4):

    - only the owner's commits count (author email = `_owner`), and each
      landed commit counts for one problem at most;
    - sessions on a feature branch belong to that branch, one problem (one
      PR); it is solved when it reaches the default branch: a merge commit
      names it ("Merge pull request #N from x/<branch>", "Merge branch
      '<branch>'"), or the branch's tip is in the default branch; it counts
      when one of the commits it landed is the owner's;
    - a session on the default branch is one problem; it is solved when a
      commit lands on the default branch between its start and LAND_GRACE
      after its end; a commit inside several sessions' windows goes to the
      nearest one (the one it is closest to the end of, then the latest
      started);
    - human-in-the-loop turns of a PR: the user turns of every session on
      its branch that started before it landed;
    - quality guard: reverted when a later default-branch commit says
      "This reverts commit <sha>" of one of its commits; re-fixed when within
      REFIX_DAYS a default-branch commit whose subject starts with "fix"
      touches a file it touched.

    Only problems that landed on or after `since` are returned."""
    tops: dict = {}
    by_repo: dict[str, list[SessionScan]] = defaultdict(list)
    for s in sessions:
        if s.started_at and (top := _toplevel(s.project_path, tops)):
            by_repo[top].append(s)
    solved: list[Solved] = []
    for root, repo_sessions in by_repo.items():
        h = _history(root, since - SESSION_LOOKBACK)
        if h is None:
            continue
        owner = _owner(h.repo)
        on_branch: dict[str, list[SessionScan]] = defaultdict(list)
        for s in repo_sessions:
            if s.git_branch and s.git_branch not in (h.default, "HEAD"):
                on_branch[s.git_branch].append(s)
        landed: dict[str, tuple[datetime, list[str], set[str]] | None] = {}   # None: landed, not the owner's
        claimed: set[str] = set()
        for c in h.commits:
            m = _MERGE_PR.match(c.subject) or _MERGE_BRANCH.match(c.subject)
            if m and len(c.parents) > 1 and m.group(1) in on_branch and m.group(1) not in landed:
                shas, files, authors = _merged_range(h, c)
                claimed.update(shas)
                landed[m.group(1)] = (c.at, shas, files) if owner in authors else None
        for branch, branch_sessions in on_branch.items():
            if branch in landed:
                continue
            tip = h.repo.resolve(f"refs/heads/{branch}") or h.repo.resolve(f"refs/remotes/origin/{branch}")
            start = min(s.started_at for s in branch_sessions)
            # A tip from before the first session is old work, not this branch's.
            if tip is None or datetime.fromisoformat(h.repo.out("show", "-s", "--format=%cI", tip)) < start:
                continue
            if not any(h.repo.ok("merge-base", "--is-ancestor", tip, ref) for ref in h.refs):
                continue
            c = next((c for c in h.commits if c.at >= start
                      and (c.sha == tip or h.repo.ok("merge-base", "--is-ancestor", tip, c.sha))), None)
            if c is not None and c.sha not in claimed and c.author == owner:
                claimed.add(c.sha)
                landed[branch] = (c.at, [c.sha], set(c.files))
        for branch, hit in landed.items():
            if hit is None:
                continue
            at, shas, files = hit
            turns = sum(s.user_turns for s in on_branch[branch] if s.started_at <= at)
            solved.append(Solved(root, branch, at, shas, files, turns))
        on_default = [s for s in repo_sessions if not s.git_branch or s.git_branch == h.default]
        credited: dict[int, list[Commit]] = defaultdict(list)
        for c in h.commits:
            if c.sha in claimed or c.author != owner:
                continue
            near = [i for i, s in enumerate(on_default) if s.started_at <= c.at <= s.ended_at + LAND_GRACE]
            if near:
                credited[min(near, key=lambda i: (max(c.at - on_default[i].ended_at, timedelta(0)),
                                                  c.at - on_default[i].started_at))].append(c)
        for i, hits in credited.items():
            solved.append(Solved(root, None, hits[0].at, [c.sha for c in hits],
                                 set().union(*(c.files for c in hits)), on_default[i].user_turns))
        for p in solved:
            if p.repo != root:
                continue
            later = [c for c in h.commits if c.at > p.landed_at and c.sha not in p.commits]
            p.reverted = any(any(sha.startswith(r) for r in _REVERTS.findall(c.body) for sha in p.commits)
                             for c in later)
            p.refixed = any(_FIX.match(c.subject) and c.files & p.files
                            for c in later if c.at <= p.landed_at + timedelta(days=REFIX_DAYS))
    return [p for p in solved if p.landed_at >= since]


def _ratio(num: float, den: float) -> float | None:
    return round(num / den, 2) if den else None


def daily_metrics(claude_dir: Path, start: date, end: date) -> dict[str, dict]:
    """One row per day from `start` to `end` (inclusive) that had sessions or
    solved problems."""
    since = datetime.combine(start, datetime.min.time(), UTC)
    sessions = [s for s in scan_all(claude_dir, (start - SESSION_LOOKBACK).isoformat())
                if s.date <= end.isoformat()]
    solved = problems_solved(sessions, since)
    days: dict[str, dict] = defaultdict(lambda: {
        "user_turns": 0, "corrections": 0, "by_detector": dict.fromkeys(DETECTORS, 0), "tokens": 0,
        "cost_usd": None, "problems_solved": 0, "reverted": 0, "refixed": 0, "pr_turns": [],
    })
    for s in sessions:
        if s.date < start.isoformat():
            continue
        d = days[s.date]
        d["user_turns"] += s.user_turns
        d["corrections"] += len(s.cases)
        for c in s.cases:
            d["by_detector"][c.detector.replace("-", "_")] += 1
        d["tokens"] += s.tokens
        if s.cost_usd is not None:
            d["cost_usd"] = round((d["cost_usd"] or 0.0) + s.cost_usd, 4)
    for p in solved:
        day = p.landed_at.date().isoformat()
        if day > end.isoformat():
            continue
        d = days[day]
        d["problems_solved"] += 1
        d["reverted"] += p.reverted
        d["refixed"] += p.refixed
        if p.branch is not None:
            d["pr_turns"].append(p.user_turns)
    out = {}
    for day, d in sorted(days.items()):
        pr_turns = d.pop("pr_turns")
        n = d["problems_solved"]
        out[day] = {
            "date": day, **d,
            "corrections_per_100_turns": _ratio(d["corrections"] * 100, d["user_turns"]),
            "tokens_per_problem": _ratio(d["tokens"], n),
            "turns_per_problem": _ratio(d["user_turns"], n),
            "cost_per_problem": _ratio(d["cost_usd"], n) if d["cost_usd"] is not None else None,
            "hitl_turns_per_pr": round(statistics.mean(pr_turns), 2) if pr_turns else None,
        }
    return out


# ── the field check ───────────────────────────────────────────────────────────

def _median(days: dict[str, dict], key: str, first: date, last: date) -> float | None:
    values = [d[key] for day, d in days.items()
              if first.isoformat() <= day <= last.isoformat() and d.get(key) is not None]
    return round(statistics.median(values), 2) if values else None


def window(days: dict[str, dict], reflections: list[dict], first: date, last: date) -> dict:
    """Rolling-median summary of the days from `first` to `last`."""
    inside = [d for day, d in days.items() if first.isoformat() <= day <= last.isoformat()]
    logs = {r["date"]: r for r in reflections if first.isoformat() <= r.get("date", "") <= last.isoformat()}
    return {
        "first": first.isoformat(), "last": last.isoformat(),
        "user_turns": sum(d["user_turns"] for d in inside),
        "problems_solved": sum(d["problems_solved"] for d in inside),
        "corrections_per_100_turns": _median(days, "corrections_per_100_turns", first, last),
        "tokens_per_problem": _median(days, "tokens_per_problem", first, last),
        "hitl_turns_per_pr": _median(days, "hitl_turns_per_pr", first, last),
        "friction_half_life_days": friction_half_life(logs),
    }


def _change(before: float | None, after: float | None) -> float | None:
    if before is None or after is None:
        return None
    if before == 0:
        return 0.0 if after == 0 else float("inf")
    return (after - before) / before


def _describe(name: str, before, after, unit: str = "") -> str:
    change = _change(before, after)
    pct = "" if change is None or change == float("inf") else f" ({change:+.0%})"
    return f"{name} {before if before is not None else 'n/a'} → {after if after is not None else 'n/a'}{unit}{pct}"


def verdict(before: dict, after: dict) -> tuple[str, str]:
    """helped / no_change / worse, and why (the accept rule's field half)."""
    corr = _change(before["corrections_per_100_turns"], after["corrections_per_100_turns"])
    tokens = _change(before["tokens_per_problem"], after["tokens_per_problem"])
    b_half, a_half = before["friction_half_life_days"], after["friction_half_life_days"]
    reason = "; ".join([
        _describe("corrections per 100 user turns", before["corrections_per_100_turns"],
                  after["corrections_per_100_turns"]),
        _describe("tokens per problem solved", before["tokens_per_problem"], after["tokens_per_problem"]),
        _describe("human-in-the-loop turns per PR", before["hitl_turns_per_pr"], after["hitl_turns_per_pr"]),
        _describe("friction half-life", b_half, a_half, " days"),
    ])
    if corr is None:
        return "no_change", f"too little data (no user turns in a window): {reason}"
    if corr > FLAT or (tokens is not None and tokens > TOKENS_WORSE):
        return "worse", reason
    half_down = b_half is None or a_half is None or a_half < b_half
    if corr < -FLAT and half_down and (tokens is None or tokens <= FLAT):
        return "helped", reason
    return "no_change", reason


def field_check(claude_dir: Path, reflections: list[dict], merged_at: datetime) -> dict:
    """The 7 days before the merge against the last 7 of the 14 days after it."""
    merged = merged_at.astimezone(UTC).date()
    end = merged + timedelta(days=FIELD_DAYS)
    days = daily_metrics(claude_dir, merged - timedelta(days=ROLLING_DAYS), end - timedelta(days=1))
    before = window(days, reflections, merged - timedelta(days=ROLLING_DAYS), merged - timedelta(days=1))
    after = window(days, reflections, end - timedelta(days=ROLLING_DAYS), end - timedelta(days=1))
    result, reason = verdict(before, after)
    return {"verdict": result, "reason": reason, "before": before, "after": after}
