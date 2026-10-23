"""
The Gardener loop (build step 5), on temp git repos, a temp home dir and temp
SQLite files, with a scripted fake model: no model, no network, and nothing
outside the temp dir is read or written (git runs with an empty global config).

The step-5 checks (docs/plan/cloud-architecture.md §12):
  - an approved proposal produces exactly one commit on one branch, even if
    the laptop crashes mid-apply or reconnects and hears the decision again
  - a proposal for a disallowed path is refused after approval
  - a rejection note reaches the next run (the resumed run, and the next
    day's run)
  - a measure job fires after merge and records its result
and the decisions behind them (§5.5): files outside a repo go through wisen's
home repo; proposals expire after 7 days; a day's proposals arrive as one
batch with the morning briefing. Plus the verification loop's numbers (§5.4)
and the replay before approval, through the runner interface with a fake.

Run: python3 -m tests.test_gardener
"""
import asyncio
import json
import os
import sqlite3
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="wisen-gardener-"))
HOME = _TMP / "laptop" / "home"   # each check group gets its own home dir and repos (`world`)
DEV = _TMP / "laptop" / "dev"
(_TMP / "gitconfig").write_text("")
os.environ.update({
    "WISEN_DB_PATH": str(_TMP / "laptop.db"),
    "WISEN_USER_ID": "local",
    "WISEN_EGRESS_LOG": str(_TMP / "egress.log"),
    "WISEN_ROOT_DIR": str(HOME),
    "GIT_SCAN_DIRS": str(DEV),
    "WISEN_GARDENER_HOME_REPO": str(_TMP / "laptop" / "home.git"),
    "WISEN_GARDENER_REPLAY_CASES": "2",
    "WISEN_OWNER_EMAIL": "dev@example.invalid",   # the fixtures' commits are the owner's unless they say otherwise
    "WISEN_MODEL": "test",
    "PYDANTIC_AI_NO_BANNER": "1",
    # Never the owner's git config, hooks or templates.
    "GIT_CONFIG_GLOBAL": str(_TMP / "gitconfig"),
    "GIT_CONFIG_NOSYSTEM": "1",
})
os.environ.pop("GITHUB_TOKEN", None)

import httpx
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from wisen import jobs as jobs_mod
from wisen import store as store_mod
from wisen.cloud import worker
from wisen.cloud.client import pair
from wisen.cloud.mailbox import Mailbox
from wisen.cloud.server import create_app
from wisen.evals import runner, verification
from wisen.evals.cases import Case, ContextTurn, save_case
from wisen.gardener import loop
from wisen.gardener.agent import Deps, build_agent
from wisen.gardener.git import Repo, one_file_diff
from wisen.gardener.targets import Refused, allowed, editable_files, resolve
from wisen.jobs import LEASE, JobEngine
from wisen.proposals import Proposals
from wisen.store import SqliteStore

T0 = datetime.now(UTC).replace(microsecond=0)
DAY = (T0 - timedelta(days=1)).date().isoformat()
CLAUDE_MD = "# Demo\n\nA small demo app.\n"
HOME_MD = "# My rules\n\n- Be brief.\n"
NOTE = "too vague: name the actual test command"


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class Crash(BaseException):
    """The process dies here (a BaseException, like SIGKILL to the job: no handler catches it)."""


# ── fixtures: repos, a home dir, transcripts ──────────────────────────────────

def git(cwd: Path, *args: str, at: datetime | None = None, email: str = "dev@example.invalid") -> str:
    stamp = f"{int((at or T0 - timedelta(days=30)).timestamp())} +0000"
    env = {**os.environ, "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": email,
           "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.invalid",
           "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    return subprocess.run(["git", "-C", str(cwd), *args], env=env, capture_output=True, text=True,
                          check=True).stdout.strip()


def make_repo(path: Path) -> Path:
    (path / "src").mkdir(parents=True)
    (path / "CLAUDE.md").write_text(CLAUDE_MD)
    (path / "src" / "app.py").write_text("print('hi')\n")
    git(path, "init", "-q", "-b", "main")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def gardener_refs(repo: Path, git_dir: Path | None = None) -> list[str]:
    r = Repo(repo, git_dir)
    return r.out("for-each-ref", "--format=%(refname)", "refs/heads/wisen/gardener").split()


def transcript(project: Path, session: str, start: datetime, branch: str, user_texts: list[str],
               tokens: int) -> None:
    """A Claude Code session: user turns 5 minutes apart, one assistant reply each."""
    events = []
    for i, text in enumerate(user_texts):
        at = start + timedelta(minutes=5 * i)
        common = {"sessionId": session, "cwd": str(project), "gitBranch": branch}
        events.append({**common, "type": "user", "timestamp": at.isoformat(),
                       "message": {"role": "user", "content": text}})
        events.append({**common, "type": "assistant", "timestamp": (at + timedelta(minutes=1)).isoformat(),
                       "message": {"role": "assistant", "id": f"{session}-{i}", "content": [
                           {"type": "text", "text": "ok"}],
                           "usage": {"input_tokens": tokens // len(user_texts), "output_tokens": 0}}})
    d = HOME / ".claude" / "projects" / ("-" + project.name)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session}.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")


def reflection(date: str) -> dict:
    mood = {"label": "iterating", "summary": "small loops", "evidence": "a quote"}
    return {
        "date": date, "alignment_score": 0.4, "alignment_label": "friction", "insight": "tests keep failing",
        "friction_points": [{"type": "unclear-prompt", "description": "agents guess the test command",
                             "evidence": "how do I run the tests?"}],
        "recommendations": [{"target": "claude-md", "action": "Say how to run the tests",
                             "reason": "agents asked three times"}],
        "mind_map_nodes": [], "mind_map_edges": [], "your_mood": mood, "agent_mood": mood,
        "your_question": {"question": "why?", "evidence": "q"}, "agent_question": {"question": "why?", "evidence": "q"},
        "config_diffs": [], "friction_deltas": [], "model_usage": [], "acted_on_recommendations": [],
        "seven_day_pattern": [], "synthesis_context": "a day",
    }


# ── the scripted model ────────────────────────────────────────────────────────

class Script:
    """A fake model: each run of the Gardener takes the next list of
    responses. Every request it receives is kept, so a test can read what
    the model was told."""

    def __init__(self):
        self.runs: list[list[ModelResponse]] = []
        self.seen: list[list] = []
        self._current: list[ModelResponse] = []

    def model(self) -> FunctionModel:
        def respond(messages: list, info: AgentInfo) -> ModelResponse:
            self.seen.append(messages)
            if len([m for m in messages if isinstance(m, ModelResponse)]) == 0 or not self._current:
                self._current = list(self.runs.pop(0)) if self.runs else [ModelResponse(parts=[TextPart("done")])]
            return self._current.pop(0)
        return FunctionModel(respond)

    def prompt_of(self, messages: list) -> str:
        return "\n".join(p.content for m in messages if isinstance(m, ModelRequest)
                         for p in m.parts if isinstance(p, UserPromptPart))


def propose(call_id: str, target: str, old: str, new: str, rationale: str = "Agents guess the test command.") -> ToolCallPart:
    return ToolCallPart("propose_change", {"target": target, "old_text": old, "new_text": new,
                                           "rationale": rationale, "evidence": ["how do I run the tests?"]},
                        tool_call_id=call_id)


def first_run(script: Script) -> None:
    script.runs.append([
        ModelResponse(parts=[ToolCallPart("list_files", {}, tool_call_id="l1"),
                             ToolCallPart("read_file", {"target": "demo:CLAUDE.md"}, tool_call_id="r1")]),
        # The allow-list refuses this at draft time; the model gets a retry.
        ModelResponse(parts=[propose("bad", "demo:src/app.py", "print('hi')", "print('bye')")]),
        ModelResponse(parts=[
            propose("c-demo", "demo:CLAUDE.md", "A small demo app.\n", "A small demo app.\n\nRun tests: make test\n"),
            propose("c-home", "home:.claude/CLAUDE.md", "- Be brief.\n", "- Be brief.\n- Name the test command.\n"),
        ]),
    ])


# ── setup ─────────────────────────────────────────────────────────────────────

def world(name: str) -> None:
    """A fresh home dir (with a global CLAUDE.md) and a dev dir holding one
    repo, `demo`; wisen reads both from the environment on every call."""
    global DEV, HOME
    DEV, HOME = _TMP / name / "dev", _TMP / name / "home"
    os.environ.update({"WISEN_ROOT_DIR": str(HOME), "GIT_SCAN_DIRS": str(DEV),
                       "WISEN_GARDENER_HOME_REPO": str(_TMP / name / "home.git")})
    make_repo(DEV / "demo")
    (HOME / ".claude").mkdir(parents=True)
    (HOME / ".claude" / "CLAUDE.md").write_text(HOME_MD)


def _setup(name: str) -> tuple[SqliteStore, JobEngine, Clock, Script]:
    os.environ["WISEN_DB_PATH"] = str(_TMP / f"{name}.db")
    store_mod._store = None
    store = store_mod.get_store()
    clock = Clock()
    engine = JobEngine(store, loop.HANDLERS, clock=clock)
    jobs_mod._engine = engine
    script = Script()
    loop.gardener_model = script.model
    # Never the real replay runner or the package's case bank (on the owner's
    # machine it holds mined cases, and replaying them runs `claude -p`).
    loop.load_cases = list
    loop.replay_case = no_real_replay
    return store, engine, clock, script


async def no_real_replay(case, variant, k):
    raise AssertionError("a test reached the replay runner")


async def drain(engine: JobEngine) -> None:
    while await engine.run_once():
        pass


async def garden(store, engine, day: str) -> dict:
    await store.upsert_reflection(reflection(day))
    job = await engine.enqueue("garden", {"date": day}, request_id=f"garden:{day}")
    done = await engine.run_until_done(job.id)
    assert done.state == "succeeded", done.error
    return done.result


def by_target(proposals: list) -> dict:
    return {p.target: p for p in proposals}


def _rows(store: SqliteStore, sql: str, *params) -> list:
    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()


# ── the checks ────────────────────────────────────────────────────────────────

async def check_propose_and_batch(store, engine, clock, script) -> dict:
    first_run(script)
    result = await garden(store, engine, DAY)
    assert result["proposals"] == 2, result
    made = by_target(await Proposals(store).list(states=("proposed",)))
    assert set(made) == {"demo:CLAUDE.md", "home:.claude/CLAUDE.md"}, made
    # The draft-time allow-list told the model why the code file was refused.
    retry = [p for m in script.seen[2] for p in getattr(m, "parts", [])
             if isinstance(p, RetryPromptPart | ToolReturnPart) and p.tool_call_id == "bad"]
    assert retry and "may not change" in str(retry[0].content), retry
    for p in made.values():
        assert p.payload["batch"] == DAY and p.payload["run_id"] == result["run_id"]
        # Q10: a proposal expires 7 days after it is made.
        assert datetime.fromisoformat(p.expires_at) == clock.now + timedelta(days=7)
        assert "Replay" in p.payload["eval_summary"]
        assert "human-in-the-loop turns per PR" in p.payload["eval_summary"], p.payload["eval_summary"]
    assert "+Run tests: make test" in made["demo:CLAUDE.md"].payload["diff"]
    (run,) = _rows(store, "SELECT state FROM gardener_runs WHERE id = ?", result["run_id"])
    assert run["state"] == "paused"
    print("✓ gardener: a run pauses on its approval calls; each becomes a 7-day proposal in the day's batch")
    return made


async def check_apply_exactly_once(store, engine, clock, made) -> None:
    demo = DEV / "demo"
    proposals = Proposals(store, clock)
    p = made["demo:CLAUDE.md"]
    await proposals.decide(p.id, "approved", request_id="tap-demo", actor="web:vela")
    await loop.on_decided(store, engine, p.id)
    # The laptop dies after the branch ref is written, before the step is recorded.
    real = Repo.create_ref

    def crash_after(self, ref, sha):
        real(self, ref, sha)
        raise Crash

    Repo.create_ref = crash_after
    try:
        await engine.run_once()
        raise AssertionError("expected the crash")
    except Crash:
        pass
    finally:
        Repo.create_ref = real
    assert gardener_refs(demo) == [f"refs/heads/wisen/gardener/{p.id}"]
    # It comes back: the lease expires and the job resumes from its journal.
    clock.now += 2 * LEASE
    await drain(engine)
    # And it reconnects: the decision feed is heard again, from the start.
    for _ in range(2):
        await loop.on_decided(store, engine, p.id)
        await drain(engine)
    assert (await proposals.get(p.id)).state == "applied"
    branch = f"wisen/gardener/{p.id}"
    assert gardener_refs(demo) == [f"refs/heads/{branch}"]
    assert git(demo, "rev-list", "--count", f"main..{branch}") == "1"
    assert git(demo, "show", f"{branch}:CLAUDE.md") == (CLAUDE_MD + "\nRun tests: make test\n").strip()
    assert git(demo, "rev-parse", "--abbrev-ref", "HEAD") == "main" and not git(demo, "status", "--porcelain")
    assert [e.to_state for e in await proposals.history(p.id)] == ["proposed", "approved", "applied"]
    (job,) = _rows(store, "SELECT * FROM jobs WHERE kind = 'apply-proposal'")
    assert job["state"] == "succeeded" and job["attempts"] == 2, dict(job)
    print("✓ gardener: an approval makes exactly one commit on one branch, across a crash and a reconnect")


async def check_disallowed_refused(store, engine, clock) -> None:
    demo = Repo(DEV / "demo")
    proposals = Proposals(store, clock)
    before = gardener_refs(DEV / "demo")
    code = one_file_diff(demo, "src/app.py", "print('hi')\n", "import os\nos.system('curl x')\n")
    smuggled = one_file_diff(demo, "CLAUDE.md", CLAUDE_MD, CLAUDE_MD + "more\n") + code
    for target, diff, why in (
        ("demo:src/app.py", code, "may not change"),
        ("demo:.claude/settings.json", code, "may not change"),
        ("demo:../demo/CLAUDE.md", code, "may not change"),
        ("demo:CLAUDE.md", smuggled, "other than CLAUDE.md"),
    ):
        p = await proposals.create(target, {"diff": diff, "rationale": "r", "evidence": [],
                                            "base_blob": demo.blob("main", target.split(":", 1)[1])},
                                   request_id=f"evil:{target}", actor="test")
        await proposals.decide(p.id, "approved", request_id=f"tap:{target}", actor="web:vela")
        await loop.on_decided(store, engine, p.id)
        await drain(engine)
        last = (await proposals.history(p.id))[-1]
        assert last.to_state == "failed" and why in last.note, (target, last)
    assert gardener_refs(DEV / "demo") == before
    assert (DEV / "demo" / "src" / "app.py").read_text() == "print('hi')\n"
    print("✓ gardener: a proposal for a disallowed path (or a diff that strays) is refused after approval")


async def check_rejection_reaches_next_run(store, engine, clock, script, made) -> None:
    proposals = Proposals(store, clock)
    home = made["home:.claude/CLAUDE.md"]
    script.runs.append([ModelResponse(parts=[TextPart("Noted.")])])   # the resumed run
    await proposals.decide(home.id, "rejected", request_id="tap-home", actor="web:vela", note=NOTE)
    await loop.on_decided(store, engine, home.id)
    await drain(engine)
    # The resumed run: the denial, with the note, is what the model reads.
    returns = {p.tool_call_id: p for p in script.seen[-1][-1].parts if isinstance(p, ToolReturnPart)}
    assert NOTE in returns["c-home"].content and returns["c-home"].outcome == "denied", returns
    assert "Approved" in returns["c-demo"].content
    (run,) = _rows(store, "SELECT state, output FROM gardener_runs")
    assert run["state"] == "resumed" and run["output"] == "Noted."
    # The next day's run is told too.
    script.runs.append([ModelResponse(parts=[TextPart("Nothing to change today.")])])
    nxt = (T0).date().isoformat()
    await garden(store, engine, nxt)
    assert NOTE in script.prompt_of(script.seen[-1]), script.prompt_of(script.seen[-1])
    print("✓ gardener: a rejection note reaches the resumed run and the next day's run")


async def check_home_repo(store, engine, clock, script) -> None:
    """Q6: a file outside any repo gets a branch in wisen's home repo; the
    owner's merge writes the file."""
    proposals = Proposals(store, clock)
    script.runs.append([ModelResponse(parts=[
        propose("c-home2", "home:.claude/CLAUDE.md", "- Be brief.\n", "- Be brief.\n- Run `make test`.\n")])])
    day = (T0 + timedelta(days=1)).date().isoformat()
    await garden(store, engine, day)
    p = by_target(await proposals.list(states=("proposed",)))["home:.claude/CLAUDE.md"]
    await proposals.decide(p.id, "approved", request_id="tap-home2", actor="web:vela")
    await loop.on_decided(store, engine, p.id)
    await drain(engine)
    assert (await proposals.get(p.id)).state == "applied"
    live = HOME / ".claude" / "CLAUDE.md"
    assert live.read_text() == HOME_MD  # nothing changes until the owner merges
    git_dir = Path(os.environ["WISEN_GARDENER_HOME_REPO"])
    await asyncio.to_thread(subprocess.run, ["git", "--git-dir", str(git_dir), "merge", "-q", "--ff-only",
                                             f"wisen/gardener/{p.id}"], check=True, capture_output=True)
    assert live.read_text() == HOME_MD + "- Run `make test`.\n"
    await worker.tend(store, engine)
    assert (await proposals.get(p.id)).state == "merged"
    print("✓ gardener: home files go through wisen's own repo; the owner's merge writes the file")


def measure_transcripts() -> None:
    """Before the merge: 3 corrections in 10 turns and 30k tokens for one
    solved problem; two weeks after: 1 in 10 and 20k."""
    demo = DEV / "demo"
    before, after = T0 - timedelta(days=3), T0 + timedelta(days=10)
    for name, start, corrections, tokens in (("before", before, 3, 30_000), ("after", after, 1, 20_000)):
        texts = [f"no, that's wrong {i}" for i in range(corrections)] + [f"next step {i}" for i in range(10 - corrections)]
        transcript(demo, f"s-{name}", start, "main", texts, tokens)
        (demo / f"{name}.txt").write_text(name)
        git(demo, "add", f"{name}.txt")
        git(demo, "commit", "-q", "-m", f"feat: {name}", at=start + timedelta(minutes=20))


async def check_measure_after_merge(store, engine, clock) -> None:
    proposals = Proposals(store, clock)
    (p,) = await proposals.list(states=("applied",))
    git(DEV / "demo", "merge", "-q", "--ff-only", f"wisen/gardener/{p.id}")
    measure_transcripts()
    await worker.tend(store, engine)
    assert (await proposals.get(p.id)).state == "merged"
    (timer,) = _rows(store, "SELECT * FROM jobs WHERE request_id = ?", f"measure:{p.id}")
    assert datetime.fromisoformat(timer["run_after"]) == clock.now + timedelta(days=14), dict(timer)
    await drain(engine)
    assert (await proposals.get(p.id)).state == "merged"  # not due yet
    clock.now += timedelta(days=14, minutes=1)
    await drain(engine)
    assert (await proposals.get(p.id)).state == "kept"
    (m,) = _rows(store, "SELECT * FROM proposal_measures WHERE proposal_id = ?", p.id)
    result = json.loads(m["result"])
    assert m["verdict"] == "helped", m["reason"]
    assert result["before"]["corrections_per_100_turns"] == 30.0 and result["after"]["corrections_per_100_turns"] == 10.0
    assert result["before"]["tokens_per_problem"] == 30_000 and result["after"]["tokens_per_problem"] == 20_000
    assert "corrections per 100 user turns 30.0 → 10.0" in m["reason"], m["reason"]
    assert "human-in-the-loop turns per PR n/a → n/a" in m["reason"], m["reason"]   # no PRs, only main
    # The next run learns the outcome and its reason.
    prompt = await loop.build_prompt(store, (T0 + timedelta(days=15)).date().isoformat())
    assert "measured helped: corrections per 100 user turns 30.0 → 10.0" in prompt, prompt
    print("✓ gardener: a merge sets a 2-week timer; the measure job records helped and keeps the change")


async def check_worse_drafts_revert(store, engine, clock) -> None:
    proposals = Proposals(store, clock)
    (p,) = await proposals.list(states=("merged",))   # the home change
    real = verification.field_check
    verification.field_check = lambda *a: {
        "verdict": "worse", "reason": "tokens per problem solved 100 → 150 (+50%)",
        "before": {}, "after": {}}
    try:
        clock.now += timedelta(days=15)
        await drain(engine)
    finally:
        verification.field_check = real
    assert (await proposals.get(p.id)).state == "revert_proposed"
    (revert,) = [r for r in await proposals.list(states=("proposed",)) if r.payload.get("reverts") == p.id]
    assert "-- Run `make test`." in revert.payload["diff"] and "+50%" in revert.payload["rationale"]
    print("✓ gardener: a worse measurement moves to revert_proposed and drafts the revert as a new proposal")


# ── the verification loop's numbers ───────────────────────────────────────────

def check_problems_solved() -> None:
    repo = make_repo(DEV / "shop")
    start = T0 - timedelta(days=2)
    transcript(repo, "feat-1", start, "feat-x", ["add the cart", "no, keep it simple", "ok"], 9_000)
    transcript(repo, "feat-2", start + timedelta(hours=3), "feat-x", ["now tests"], 3_000)
    transcript(repo, "main-1", start + timedelta(hours=6), "main", ["bump version"], 2_000)
    # A Q&A session open at the same time: the bump is credited to one of them only.
    transcript(repo, "main-2", start + timedelta(hours=6, minutes=2), "main", ["what is in VERSION?"], 1_000)
    # A teammate's commit lands during this session: not the owner's problem.
    transcript(repo, "main-3", start + timedelta(hours=3), "main", ["what does the cart do?"], 1_000)
    # A branch that never landed.
    transcript(repo, "feat-y-1", start + timedelta(hours=2), "feat-y", ["try a wishlist"], 1_000)
    git(repo, "checkout", "-q", "-b", "feat-y")
    (repo / "wish.py").write_text("wish\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "feat: wishlist", at=start + timedelta(hours=2, minutes=10))
    git(repo, "checkout", "-q", "main")
    git(repo, "checkout", "-q", "-b", "feat-x")
    (repo / "cart.py").write_text("cart\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "feat: cart", at=start + timedelta(hours=1))
    git(repo, "checkout", "-q", "main")
    (repo / "NOTES").write_text("mate\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "docs: notes", at=start + timedelta(hours=3, minutes=30),
        email="mate@example.invalid")
    git(repo, "merge", "-q", "--no-ff", "feat-x", "-m", "Merge branch 'feat-x'", at=start + timedelta(hours=4))
    merge = git(repo, "rev-parse", "HEAD")
    (repo / "VERSION").write_text("2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "chore: bump", at=start + timedelta(hours=6, minutes=5))
    git(repo, "revert", "--no-edit", "-m", "1", merge, at=start + timedelta(days=1))
    days = verification.daily_metrics(HOME / ".claude" / "projects", start.date(), start.date())
    d = days[start.date().isoformat()]
    assert d["problems_solved"] == 2, d                # the merged branch, and the bump, once
    assert d["hitl_turns_per_pr"] == 4.0, d            # 3 + 1 user turns across the branch's sessions
    assert d["by_detector"]["explicit_correction"] == 1 and d["user_turns"] == 8, d
    assert d["tokens"] == 17_000 and d["tokens_per_problem"] == 8_500.0, d
    assert d["reverted"] == 1, d                       # the merge was reverted the next day
    print("✓ verification: problems solved (the owner's commits, each once), human turns per PR, tokens per "
          "problem, reverted share")


def check_symlink_targets() -> None:
    """A symlink in a repo (AGENTS.md -> CLAUDE.md, or a linked .claude dir)
    is neither offered nor accepted: a diff for it could never apply."""
    repo = make_repo(DEV / "links")
    (repo / "AGENTS.md").symlink_to("CLAUDE.md")
    (repo / "shared").mkdir()
    (repo / ".claude").symlink_to("shared")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "links")
    repos = {"links": repo}
    assert editable_files(repos) == [f for f in editable_files(repos) if f.startswith("home:")] + ["links:CLAUDE.md"]
    for target in ("links:AGENTS.md", "links:.claude/skills/x/SKILL.md"):
        try:
            resolve(target, repos)
            raise AssertionError(f"{target} was not refused")
        except Refused as e:
            assert "symlink" in str(e), e
    assert resolve("links:CLAUDE.md", repos).current() == CLAUDE_MD
    print("✓ gardener: symlinks in a repo are neither offered nor accepted")


async def check_one_proposal_per_file_per_run() -> None:
    """Two changes to one file in one run: the second is sent back, so each
    file has at most one open proposal."""
    edit = lambda cid, old, new: propose(cid, "links:CLAUDE.md", old, new)
    responses = [ModelResponse(parts=[edit("d1", "A small demo app.\n", "A demo.\n"),
                                      edit("d2", "# Demo\n", "# The demo\n")])]
    seen = []

    def respond(messages, info):
        seen.append(messages)
        return responses.pop(0)

    deps = Deps(repos={"links": DEV / "links"})
    result = await build_agent(FunctionModel(respond)).run("go", deps=deps)
    assert [c.tool_call_id for c in result.output.approvals] == ["d1"], result.output
    assert list(deps.drafts) == ["d1"] and "links:CLAUDE.md" in deps.open_targets, deps
    print("✓ gardener: one run proposes at most one change per file")


def check_verdicts() -> None:
    w = lambda c, t, h=None: {"corrections_per_100_turns": c, "tokens_per_problem": t,
                              "friction_half_life_days": h, "hitl_turns_per_pr": 3}
    assert verification.verdict(w(4, 100, 3), w(2, 100, 2))[0] == "helped"
    assert verification.verdict(w(4, 100), w(4.1, 100))[0] == "no_change"   # within 5%
    assert verification.verdict(w(4, 100), w(2, 120))[0] == "worse"         # tokens up 20%
    assert verification.verdict(w(4, 100), w(5, 90))[0] == "worse"          # corrections up 25%
    assert verification.verdict(w(4, 100, 2), w(2, 100, 3))[0] == "no_change"  # half-life went up
    assert verification.verdict(w(None, None), w(2, 100))[0] == "no_change"
    print("✓ verification: the field verdict follows the accept rule (corrections, half-life, tokens)")


def check_allow_list() -> None:
    ok = ["CLAUDE.md", "AGENTS.md", "docs/CLAUDE.md", ".claude/CLAUDE.md", ".claude/skills/tdd/SKILL.md",
          ".agents/skills/x/notes/a.md"]
    no = ["src/app.py", ".claude/settings.json", ".mcp.json", ".claude/skills/SKILL.md", "../CLAUDE.md",
          "/etc/CLAUDE.md", ".git/CLAUDE.md", "skills/x/SKILL.md", "a\\CLAUDE.md", ""]
    assert all(allowed(p) for p in ok), [p for p in ok if not allowed(p)]
    assert not any(allowed(p) for p in no), [p for p in no if allowed(p)]
    print("✓ gardener: the allow-list takes CLAUDE.md, AGENTS.md and skill files only")


async def check_replay_before_proposal() -> None:
    """The controlled check runs through the runner's interface; here with a
    fake that passes a case only under the proposed text. Capped per kind,
    cost recorded."""
    cases = []
    for i, kind in enumerate(["correction"] * 3 + ["clean"] * 3):
        case = Case(id=f"c{i}", date="2026-10-01", session_id="s", source_path="x", project_path=str(DEV / "demo"),
                    detector="explicit-correction" if kind == "correction" else None,
                    context=[ContextTurn(role="user", content="run the tests")], correction="no",
                    behavior_check="Did it guess the test command?", split="eval", status="confirmed", kind=kind)
        cases.append(case)
    calls = []

    async def fake(case, variant, k):
        calls.append((case.id, variant))
        knows = "make test" in json.dumps(variant.project_files)
        return {"case": case.id, "passed": knows or case.kind == "clean", "evidence": "", "tokens": 1_000,
                "cost_usd": 0.01}

    current = runner.Variant(project_files={"CLAUDE.md": CLAUDE_MD})
    proposed = runner.Variant(project_files={"CLAUDE.md": CLAUDE_MD + "Run tests: make test\n"})
    report = await runner.compare(cases, current, proposed, cap=2, run=fake)
    assert len(calls) == 8 and report["cases"] == {"eval": 2, "clean": 2}, calls   # 2 + 2 cases x 2 configs
    assert report["current"] == {"pass_rate": 0.0, "clean_pass_rate": 1.0}
    assert report["proposed"] == {"pass_rate": 1.0, "clean_pass_rate": 1.0}
    assert report["meets_rule"] is True and report["tokens"] == 8_000 and report["cost_usd"] == 0.08
    summary = loop.eval_summary(report, {"tokens_per_problem": 7000.0, "hitl_turns_per_pr": 4.0})
    assert "pass rate 0% → 100%" in summary and "accept rule" in summary and "$0.08" in summary, summary
    assert "7,000 tokens per problem solved, 4 human-in-the-loop turns per PR" in summary, summary
    # In the Gardener: the draft's cases are those of its project; the fake stands in for the runner.
    for case in cases:
        save_case(case, _TMP / "cases")
    real_load, real_case = loop.load_cases, loop.replay_case
    loop.load_cases, loop.replay_case = (lambda: cases), fake
    try:
        draft = loop.Draft(tool_call_id="x", target="demo:CLAUDE.md", diff="", base_blob=None, rationale="r",
                           evidence=[], current=CLAUDE_MD, proposed=CLAUDE_MD + "Run tests: make test\n")
        assert (await loop.replay_draft(draft))["meets_rule"] is True
    finally:
        loop.load_cases, loop.replay_case = real_load, real_case
    print("✓ replay: proposed vs current plus the clean set, capped per proposal, cost recorded, on the proposal")


# ── paired with a server: the batch push, the git states, the measure timer ───

async def check_with_server() -> None:
    world("paired")
    store, engine, clock, script = _setup("paired")
    server_clock = Clock()
    mailbox = Mailbox(_TMP / "server.db", sender=None, clock=server_clock)
    mailbox.set_user("vela", "correct horse battery")
    app = create_app(mailbox, secure_cookies=False, run_engines=False)
    base = "http://localhost"
    first_run(script)
    script.runs.append([ModelResponse(parts=[TextPart("Noted.")])])
    await store.upsert_reflection(reflection(DAY))
    await drain(engine)
    job = await engine.enqueue("garden", {"date": DAY}, request_id=f"garden:{DAY}")
    await engine.run_until_done(job.id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base) as http:
        assert (await http.post("/login", json={"user": "vela", "password": "correct horse battery"})).status_code == 200
        code = (await http.post("/api/pairing-codes", json={})).json()["code"]
        creds = await pair(base, code, "laptop", http=http)
        await worker.run(creds, store, http=http, once=True)
        # Q11: one push, the morning briefing, carrying the batch.
        pushes = [j.args for j in _jobs(mailbox, "push")]
        assert [p["title"] for p in pushes] == ["Reflection ready"], pushes
        assert pushes[0]["body"].endswith("· 2 proposals waiting"), pushes
        inbox = by_target([p for p in (await mailbox.inbox("vela")) if p.state == "proposed"])
        assert inbox["demo:CLAUDE.md"].payload["batch"] == DAY
        # Approved item by item: one yes, one no with a note.
        demo, home = inbox["demo:CLAUDE.md"].id, inbox["home:.claude/CLAUDE.md"].id
        await http.post(f"/api/proposals/{demo}/decision", json={"decision": "approved", "request_id": "tap-00001"})
        await http.post(f"/api/proposals/{home}/decision", json={"decision": "rejected", "request_id": "tap-00002",
                                                                 "note": NOTE})
        await worker.run(creds, store, http=http, once=True)   # hears both; applies; resumes
        assert NOTE in json.dumps([str(p) for p in script.seen[-1][-1].parts])
        # A reconnect replays the feed from the start: still one branch, one commit.
        worker._set(store, "decisions_cursor", "0")
        await worker.run(creds, store, http=http, once=True)
        assert gardener_refs(DEV / "demo")[-1:] == [f"refs/heads/wisen/gardener/{demo}"]
        assert (await mailbox.proposals("vela").get(demo)).state == "applied"
        # The owner merges; the laptop sees it; the server sets the measure timer.
        git(DEV / "demo", "merge", "-q", "--ff-only", f"wisen/gardener/{demo}")
        measure_transcripts()
        await worker.run(creds, store, http=http, once=True)
        assert (await mailbox.proposals("vela").get(demo)).state == "merged"
        (timer,) = [j for j in _jobs(mailbox, "measure")]
        assert timer.runner == "device" and datetime.fromisoformat(timer.run_after) == T0 + timedelta(days=14)
        # Two weeks on, the server's timer hands the laptop a measure job.
        server_clock.now = clock.now = T0 + timedelta(days=14, minutes=1)
        await worker.run(creds, store, http=http, once=True)
        await worker.run(creds, store, http=http, once=True)
        assert (await mailbox.proposals("vela").get(demo)).state == "kept"
        assert [j.state for j in _jobs(mailbox, "measure")] == ["succeeded"]
        # The next morning's run records the last two weeks of metrics, which sync up.
        await garden(store, engine, (T0 + timedelta(days=14)).date().isoformat())
        await worker.run(creds, store, http=http, once=True)
        hill = (await http.get("/api/timeline")).json()
        (change,) = hill["changes"]
        assert change["target"] == "demo:CLAUDE.md" and change["verdict"] == "helped", change
        assert any(d["problems_solved"] for d in hill["days"]), hill["days"]
    print("✓ cloud: the batch arrives with the briefing; git states and the measure result reach the server")


def _jobs(mailbox: Mailbox, kind: str) -> list:
    with sqlite3.connect(mailbox.path) as conn:
        conn.row_factory = sqlite3.Row
        return [jobs_mod.Job.from_row(r) for r in conn.execute(
            "SELECT * FROM jobs WHERE kind = ? ORDER BY created_at", (kind,))]


async def main():
    world("laptop")
    check_allow_list()
    check_verdicts()
    await check_replay_before_proposal()
    store, engine, clock, script = _setup("laptop")
    made = await check_propose_and_batch(store, engine, clock, script)
    await check_apply_exactly_once(store, engine, clock, made)
    await check_disallowed_refused(store, engine, clock)
    await check_rejection_reaches_next_run(store, engine, clock, script, made)
    await check_measure_after_merge(store, engine, clock)
    await check_home_repo(store, engine, clock, script)   # merged two weeks later, measured next
    await check_worse_drafts_revert(store, engine, clock)
    world("metrics")
    check_problems_solved()
    check_symlink_targets()
    await check_one_proposal_per_file_per_run()
    await check_with_server()
    print("\nall gardener tests passed")


if __name__ == "__main__":
    asyncio.run(main())
