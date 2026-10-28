"""
The Gardener loop as durable jobs on the laptop (wisen/jobs.py):

  garden            garden:<date>             after a day's synthesis: record the day's
                                              metrics, run the Gardener, replay each draft,
                                              and record its proposals as that day's batch
  gardener-resume   gardener-resume:<run>     once every proposal of a run is decided:
                                              resume the run with the decisions
  apply-proposal    apply:<proposal>          an approval: the branch (wisen/gardener/applier.py)
  measure-proposal  measure:<proposal>        2 weeks after merge: the field check; "worse"
                                              drafts a revert proposal
  expire-proposal   expire-proposal:<proposal>  7 days unanswered (wisen/proposals.py)

`on_decided` is how a decision (from the server's feed, the CLI, or an
expiry) turns into the next jobs. See docs/plan/cloud-architecture.md §5.
"""
import asyncio
import json
import uuid
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from pydantic import BaseModel
from pydantic_ai import (
    ConversationTypeAdapter,
    DeferredToolRequests,
    DeferredToolResults,
    ToolApproved,
    ToolDenied,
)

from wisen import config
from wisen.evals import runner, verification
from wisen.evals.cases import Case, load_cases
from wisen.gardener import applier
from wisen.gardener.agent import Deps, Draft, build_agent
from wisen.gardener.git import GitError, one_file_diff
from wisen.gardener.targets import Refused, Target, resolve
from wisen.jobs import JobContext, JobEngine, ts
from wisen.proposals import Proposals, expire_proposal_job
from wisen.store import SqliteStore

EXPIRES_AFTER = timedelta(days=7)
OPEN = ("proposed", "approved", "applied")
TENDS = ("claude-md", "agents-md")   # reflection recommendation targets the Gardener acts on


def gardener_model():
    """The model the Gardener runs on: the chat model (wisen/llm.py)."""
    from wisen.llm import get_model

    return get_model()


# The replay runner's interface (wisen/evals/runner.py); a test swaps in a fake.
replay_case: runner.RunCase = runner.run_case


def replay_cap() -> int:
    return int(config.get("gardener", "replay_cases", "WISEN_GARDENER_REPLAY_CASES", "5"))


def claude_dir() -> Path:
    return config.root() / ".claude" / "projects"


class GardenerRun(BaseModel):
    run_id: str
    conversation: str
    output: str | None
    drafts: list[Draft]


# ── reading the store ─────────────────────────────────────────────────────────

def _rows(store: SqliteStore, sql: str, *params) -> list:
    with closing(store._connect()) as conn:
        return conn.execute(sql, (store.user_id, *params)).fetchall()


def open_targets(store: SqliteStore) -> set[str]:
    return {r["target"] for r in _rows(
        store, f"SELECT target FROM proposals WHERE user_id = ? AND state IN ({', '.join('?' * len(OPEN))})", *OPEN)}


def past_outcomes(store: SqliteStore, limit: int = 50) -> list[dict]:
    """Past proposals in every state, newest first, with their content, the
    owner's decision note, when they were merged, and the measured verdict."""
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in _rows(store, """
        SELECT p.id, p.target, p.state, p.payload, p.created_at, d.decision, d.note, m.verdict, m.reason,
               (SELECT at FROM proposal_events e WHERE e.proposal_id = p.id AND e.to_state = 'merged') AS merged_at
        FROM proposals p
        LEFT JOIN proposal_decisions d ON d.proposal_id = p.id
        LEFT JOIN proposal_measures m ON m.proposal_id = p.id
        WHERE p.user_id = ? ORDER BY p.created_at DESC LIMIT ?""", limit)]


def wants_tending(reflection: dict) -> bool:
    """Run the Gardener only when the day points at an instruction file."""
    return (any(r.get("target") in TENDS for r in reflection.get("recommendations", []))
            or any(f.get("type") == "missing-skill" for f in reflection.get("friction_points", [])))


async def recent_reflections(store: SqliteStore, day: str) -> list[dict]:
    """The week the Gardener reads on `day`: that day's reflection and the six before it."""
    after = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    return await store.list_reflections(limit=7, before=after)


async def build_prompt(store: SqliteStore, day: str) -> str:
    lines = [f"Today is {day}.", "", "## Recent reflections, newest first"]
    for r in await recent_reflections(store, day):
        lines.append(f"\n### {r.get('date')} ({r.get('alignment_label', '?')})")
        for f in r.get("friction_points", []):
            lines.append(f"- friction [{f.get('type')}]: {f.get('description')} (evidence: {f.get('evidence')})")
        for rec in r.get("recommendations", []):
            lines.append(f"- recommendation [{rec.get('target')}]: {rec.get('action')} — {rec.get('reason')}")
        if r.get("insight"):
            lines.append(f"- insight: {r['insight']}")
    outcomes = await asyncio.to_thread(past_outcomes, store)
    lines += ["", "## Your past proposals and what happened to them (past_proposals has their content)"]
    for o in outcomes:
        what = o["state"]
        if o["decision"] == "rejected":
            what += f"; the owner's note: {o['note'] or '(none)'}"
        if o["verdict"]:
            what += f"; measured {o['verdict']}: {o['reason']}"
        lines.append(f"- {o['target']}: {what}")
    if not outcomes:
        lines.append("- none yet")
    return "\n".join(lines)


# ── garden: propose ───────────────────────────────────────────────────────────

async def propose(store: SqliteStore, day: str) -> GardenerRun:
    deps = Deps(open_targets=await asyncio.to_thread(open_targets, store),
                reflection_dates={r["date"] for r in await recent_reflections(store, day)},
                history=await asyncio.to_thread(past_outcomes, store))
    result = await build_agent(gardener_model()).run(await build_prompt(store, day), deps=deps)
    conversation = ConversationTypeAdapter.dump_json(result.conversation).decode()
    if isinstance(result.output, DeferredToolRequests):
        drafts = [deps.drafts[c.tool_call_id] for c in result.output.approvals if c.tool_call_id in deps.drafts]
        return GardenerRun(run_id=uuid.uuid4().hex, conversation=conversation, output=None, drafts=drafts)
    return GardenerRun(run_id=uuid.uuid4().hex, conversation=conversation, output=result.output, drafts=[])


def _cases_for(t: Target) -> list[Case]:
    if t.is_home:
        return load_cases()
    root = t.repo.work_tree.resolve()
    return [c for c in load_cases() if Path(c.project_path).resolve().is_relative_to(root)]


def _variants(t: Target, d: Draft) -> tuple[runner.Variant, runner.Variant] | None:
    if t.is_home:
        if not t.path.startswith(".claude/"):
            return None  # the replay runs Claude Code, which reads only ~/.claude from home
        rel = t.path.removeprefix(".claude/")
        files = lambda text: {rel: text} if text is not None else {}
        return runner.Variant(config_files=files(d.current)), runner.Variant(config_files=files(d.proposed))
    files = lambda text: {t.path: text} if text is not None else {}
    return runner.Variant(project_files=files(d.current)), runner.Variant(project_files=files(d.proposed))


async def replay_draft(d: Draft) -> dict | None:
    """The controlled check before approval: replay the file's cases under
    the current and the proposed text. None when there is nothing to replay."""
    cap = replay_cap()
    if cap <= 0:
        return None
    t = resolve(d.target)
    variants = _variants(t, d)
    cases = await asyncio.to_thread(_cases_for, t)
    if variants is None or not cases:
        return None
    report = await runner.compare(cases, *variants, cap=cap, run=replay_case)
    return report if report["replays"] else None


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate:.0%}"


def cost_now(store: SqliteStore, day: str) -> dict:
    """The two headline cost numbers as they stand: 7-day medians up to `day`."""
    end = date.fromisoformat(day)
    first = end - timedelta(days=verification.ROLLING_DAYS - 1)
    days = {r["date"]: json.loads(r["doc"]) for r in _rows(
        store, "SELECT date, doc FROM daily_metrics WHERE user_id = ? AND date BETWEEN ? AND ?",
        first.isoformat(), day)}
    return {k: verification._median(days, k, first, end) for k in ("tokens_per_problem", "hitl_turns_per_pr")}


def _num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,g}"


def eval_summary(report: dict | None, cost: dict) -> str:
    now = (f" Cost now (7-day medians): {_num(cost['tokens_per_problem'])} tokens per problem solved, "
           f"{_num(cost['hitl_turns_per_pr'])} human-in-the-loop turns per PR.")
    if report is None:
        return "Replay: no confirmed cases for this file yet, so the expected gain is unknown." + now
    cur, new = report["current"], report["proposed"]
    rule = {True: "yes", False: "no", None: "not enough cases to tell"}[report["meets_rule"]]
    spent = f"{report['tokens']:,} tokens" + (f", ${report['cost_usd']:.2f}" if report["cost_usd"] is not None else "")
    return (f"Replay on {report['cases']['eval']} eval and {report['cases']['clean']} clean-set cases: "
            f"pass rate {_pct(cur['pass_rate'])} → {_pct(new['pass_rate'])}; clean set "
            f"{_pct(cur['clean_pass_rate'])} → {_pct(new['clean_pass_rate'])}. Meets the accept rule "
            f"(pass rate rises, clean set holds): {rule}. Replay cost: {spent}.{now}")


def record_run(store: SqliteStore, proposals: Proposals, run: GardenerRun, replays: dict, day: str) -> list[str]:
    """The run and its proposals, in one transaction; a rerun finds them."""
    now = proposals.clock()
    cost = cost_now(store, day)
    ids = []
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO gardener_runs (id, user_id, date, conversation, state, output, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO NOTHING",
            (run.run_id, store.user_id, day, run.conversation, "paused" if run.drafts else "done", run.output,
             ts(now), ts(now)),
        )
        for d in run.drafts:
            report = replays.get(d.tool_call_id)
            payload = {"diff": d.diff, "rationale": d.rationale, "evidence": d.evidence,
                       "eval_summary": eval_summary(report, cost), "batch": day, "run_id": run.run_id,
                       "tool_call_id": d.tool_call_id, "base_blob": d.base_blob, "replay": report}
            ids.append(proposals.create_in(conn, d.target, payload, f"gardener:{run.run_id}:{d.tool_call_id}",
                                           "gardener", now + EXPIRES_AFTER).id)
    return ids


def record_metrics(store: SqliteStore, day: str, days: int = 14) -> int:
    """Recompute the last `days` days of verification metrics up to `day`."""
    end = date.fromisoformat(day)
    rows = verification.daily_metrics(claude_dir(), end - timedelta(days=days - 1), end)
    with store.transaction() as conn:
        for d, doc in rows.items():
            conn.execute(
                "INSERT INTO daily_metrics (user_id, date, doc) VALUES (?, ?, ?) "
                "ON CONFLICT (user_id, date) DO UPDATE SET doc = excluded.doc",
                (store.user_id, d, json.dumps(doc)),
            )
    return len(rows)


async def garden_job(ctx: JobContext, args: dict) -> dict:
    store, day = ctx.engine.store, args["date"]
    await ctx.step("metrics", lambda: asyncio.to_thread(record_metrics, store, day), replay_safe=True)
    reflection = await store.get_reflection(day)
    if reflection is None or not wants_tending(reflection):
        return {"date": day, "proposals": 0}
    # A proposal needs the problem on two different days, so one reflection
    # alone can propose nothing: no model run.
    if len(await recent_reflections(store, day)) < 2:
        return {"date": day, "proposals": 0, "skipped": "only one reflection so far"}
    run = await ctx.step("propose", lambda: propose(store, day), replay_safe=True, output=GardenerRun)
    replays = {}
    for d in run.drafts:
        replays[d.tool_call_id] = await ctx.step(f"replay:{d.tool_call_id}", lambda d=d: replay_draft(d),
                                                 replay_safe=True, output=dict | None)
    ids = await ctx.step("record", lambda: asyncio.to_thread(
        record_run, store, Proposals(store, ctx.engine.clock), run, replays, day), replay_safe=True, output=list[str])
    return {"date": day, "proposals": len(ids), "run_id": run.run_id}


# ── decisions ─────────────────────────────────────────────────────────────────

def _run_open(store: SqliteStore, run_id: str) -> int:
    return _rows(store, "SELECT COUNT(*) FROM proposals WHERE user_id = ? AND state = 'proposed' "
                        "AND json_extract(payload, '$.run_id') = ?", run_id)[0][0]


async def on_decided(store: SqliteStore, engine: JobEngine, proposal_id: str) -> None:
    """After a decision or an expiry: an approval queues the applier, and a
    run with no undecided proposal left queues its resume. Both are keyed by
    request id, so hearing a decision twice queues nothing new."""
    p = await Proposals(store).get(proposal_id)
    if p is None:
        return
    if p.state == "approved":
        await engine.enqueue("apply-proposal", {"proposal_id": proposal_id}, request_id=f"apply:{proposal_id}")
    run_id = p.payload.get("run_id")
    if run_id and p.state != "proposed" and not await asyncio.to_thread(_run_open, store, run_id):
        await engine.enqueue("gardener-resume", {"run_id": run_id}, request_id=f"gardener-resume:{run_id}")


async def expire_job(ctx: JobContext, args: dict) -> dict:
    result = await expire_proposal_job(ctx, args)
    if result["expired"]:
        await on_decided(ctx.engine.store, ctx.engine, args["proposal_id"])
    return result


def _results(store: SqliteStore, run_id: str) -> tuple[DeferredToolResults, dict[str, str]]:
    """Each pending call's answer: approved, or denied with why."""
    approvals, branches = {}, {}
    for r in _rows(store, """
            SELECT p.id, p.state, p.payload, d.decision, d.note FROM proposals p
            LEFT JOIN proposal_decisions d ON d.proposal_id = p.id
            WHERE p.user_id = ? AND json_extract(p.payload, '$.run_id') = ?""", run_id):
        call = json.loads(r["payload"])["tool_call_id"]
        if r["decision"] == "approved":
            approvals[call] = ToolApproved()
            branches[call] = applier.branch_name(r["id"])
        elif r["decision"] == "rejected":
            approvals[call] = ToolDenied(f"The owner rejected this proposal. Their note: {r['note'] or '(none)'}")
        elif r["state"] == "expired":
            approvals[call] = ToolDenied("Nobody decided within 7 days, so the proposal expired.")
        else:
            approvals[call] = ToolDenied(f"The proposal was not applied ({r['state']}).")
    return DeferredToolResults(approvals=approvals), branches


async def resume(store: SqliteStore, run_id: str) -> dict:
    row = _rows(store, "SELECT conversation FROM gardener_runs WHERE user_id = ? AND id = ?", run_id)[0]
    conversation = ConversationTypeAdapter.validate_json(row["conversation"])
    results, branches = await asyncio.to_thread(_results, store, run_id)
    deps = Deps(resuming=True, branches=branches)
    result = await build_agent(gardener_model()).run(conversation=conversation, deferred_tool_results=results,
                                                     deps=deps)
    output = result.output if isinstance(result.output, str) else (
        "(it asked for more changes; the next daily run may propose them)")
    return {"conversation": ConversationTypeAdapter.dump_json(result.conversation).decode(), "output": output}


def _save_resumed(store: SqliteStore, run_id: str, resumed: dict, now: str) -> None:
    with store.transaction() as conn:
        conn.execute("UPDATE gardener_runs SET conversation = ?, output = ?, state = 'resumed', updated_at = ? "
                     "WHERE user_id = ? AND id = ?", (resumed["conversation"], resumed["output"], now,
                                                       store.user_id, run_id))


async def resume_job(ctx: JobContext, args: dict) -> dict:
    store, run_id = ctx.engine.store, args["run_id"]
    state = _rows(store, "SELECT state FROM gardener_runs WHERE user_id = ? AND id = ?", run_id)
    if not state or state[0]["state"] != "paused":
        return {"run_id": run_id, "resumed": False}
    resumed = await ctx.step("resume", lambda: resume(store, run_id), replay_safe=True, output=dict)
    await ctx.step("save", lambda: asyncio.to_thread(
        _save_resumed, store, run_id, resumed, ts(ctx.engine.clock())), replay_safe=True)
    return {"run_id": run_id, "resumed": True}


# ── apply ─────────────────────────────────────────────────────────────────────

async def apply_job(ctx: JobContext, args: dict) -> dict:
    store, proposal_id = ctx.engine.store, args["proposal_id"]
    proposals = Proposals(store, ctx.engine.clock)
    p = await proposals.get(proposal_id)
    if p is None or p.state != "approved":
        return {"state": p.state if p else None}
    info = await ctx.step("check", lambda: asyncio.to_thread(applier.check, store, proposal_id), replay_safe=True,
                          output=dict)
    reason = info.get("refused")
    if reason is None:
        try:
            # Replay-safe by construction: the same journaled base and the
            # approval's time make the same commit, and the branch is created
            # only if it is absent, so a rerun cannot make a second one.
            made = await ctx.step("commit", lambda: asyncio.to_thread(
                applier.commit, info, proposal_id, p.target, p.payload["rationale"], p.payload["diff"]),
                replay_safe=True, output=dict)
        except Refused as e:
            reason = str(e)
    if reason is not None:
        await ctx.step("refuse", lambda: asyncio.to_thread(applier.record_failed, proposals, proposal_id, reason),
                       replay_safe=True)
        return {"failed": reason}
    await ctx.step("record", lambda: asyncio.to_thread(
        applier.record_applied, store, proposals, proposal_id, info, made), replay_safe=True)
    return {"branch": info["branch"], "commit": made["commit"]}


# ── measure ───────────────────────────────────────────────────────────────────

def draft_revert(target: str, diff: str, when: str) -> dict | None:
    """The change that undoes `diff` on the file as it is now, or None when
    it no longer reverses cleanly."""
    try:
        t = resolve(target)
        if t.is_home:
            applier.record_found(t, when)
        base = t.base()
        current_blob = t.repo.blob(base, t.path) if base else None
        with t.repo.scratch_index() as env:
            t.repo.run("read-tree", base, env=env)
            if not t.repo.ok("apply", "-R", "--cached", "-", env=env, input=diff):
                return None
            tree = t.repo.out("write-tree", env=env)
        reverted_blob = t.repo.blob(tree, t.path)
        current = t.repo.read_blob(current_blob) if current_blob else None
        reverted = t.repo.read_blob(reverted_blob) if reverted_blob else ""
        return {"diff": one_file_diff(t.repo, t.path, current, reverted), "base_blob": current_blob}
    except (Refused, GitError):
        return None


def record_measure(store: SqliteStore, proposals: Proposals, proposal_id: str, target: str, result: dict,
                   revert: dict | None) -> None:
    now = proposals.clock()
    worse = result["verdict"] == "worse"
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO proposal_measures (proposal_id, user_id, verdict, reason, result, at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (proposal_id) DO NOTHING",
            (proposal_id, store.user_id, result["verdict"], result["reason"], json.dumps(result), ts(now)),
        )
        proposals.transition_in(conn, proposal_id, "measuring", "revert_proposed" if worse else "kept",
                                f"measured:{proposal_id}", "measure", result["reason"][:1000])
        if worse and revert is not None:
            proposals.create_in(conn, target, {
                "diff": revert["diff"], "base_blob": revert["base_blob"], "reverts": proposal_id, "evidence": [],
                "rationale": f"Measured worse two weeks after merge, so this reverts proposal {proposal_id}: "
                             f"{result['reason']}",
            }, f"revert:{proposal_id}", "gardener", now + EXPIRES_AFTER)


async def measure_job(ctx: JobContext, args: dict) -> dict:
    store, proposal_id = ctx.engine.store, args["proposal_id"]
    proposals = Proposals(store, ctx.engine.clock)
    p = await proposals.get(proposal_id)
    if p is None or p.state not in ("merged", "measuring"):
        return {"state": p.state if p else None}
    if p.state == "merged":
        await ctx.step("start", lambda: proposals.transition(
            proposal_id, "merged", "measuring", request_id=f"measuring:{proposal_id}", actor="measure"),
            replay_safe=True)
    merged = await asyncio.to_thread(applier.merged_at, store, proposal_id)
    reflections = await store.list_reflections()
    result = await ctx.step("field-check", lambda: asyncio.to_thread(
        verification.field_check, claude_dir(), reflections, merged), replay_safe=True, output=dict)
    revert = None
    if result["verdict"] == "worse":
        when = f"{int(ctx.engine.clock().timestamp())} +0000"
        revert = await ctx.step("draft-revert", lambda: asyncio.to_thread(
            draft_revert, p.target, p.payload["diff"], when), replay_safe=True, output=dict | None)
    await ctx.step("record", lambda: asyncio.to_thread(
        record_measure, store, proposals, proposal_id, p.target, result, revert), replay_safe=True)
    return {"verdict": result["verdict"], "revert": revert is not None}


HANDLERS = {
    "garden": garden_job,
    "gardener-resume": resume_job,
    "apply-proposal": apply_job,
    "measure-proposal": measure_job,
    "expire-proposal": expire_job,
}
