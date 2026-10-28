"""
The Gardener agent: reads recent reflections and the files your coding agents
read, and proposes small changes to them.

Its one acting tool, `propose_change`, is declared `requires_approval=True`,
so a run that calls it ends with `DeferredToolRequests` instead of changing
anything. The run's `Conversation` (with those pending calls) is stored, and a
later run resumes from it with each human decision as a `ToolApproved` or a
`ToolDenied(note)`, so a rejection note reaches the model
(docs/plan/cloud-architecture.md §5.2).

Approval is not authorization: the tool's body only acknowledges. The
applier (wisen/gardener/applier.py) re-checks the approval record and the
allow-list before it writes a branch.

What may be proposed is bounded twice: the instructions say what makes a
proposal worth making (general, recurring, in the right file for its owner,
harness and load frequency, and never about wisen's own pipeline), and the
draft check enforces the part the code can see: each evidence quote names the
reflection date it comes from, a proposal needs quotes from at least two
different days, and a change already proposed for the same file (whatever
became of it) is not proposed again. Past proposals, with their content, are
the `past_proposals` tool, so the model can see when the same idea was tried
elsewhere, rejected with a note, or applied and settled.
"""
from dataclasses import dataclass, field

from pydantic import BaseModel
from pydantic_ai import Agent, DeferredToolRequests, ModelRetry, RunContext

from wisen.gardener.git import GitError
from wisen.gardener.targets import LocalFiles, Refused, SnapshotFiles
from wisen.redact import EMAIL, SECRET, redact

MAX_DIFF = 20_000   # the proposal packet's cap on a diff (wisen/packets.py)
MAX_READ = 20_000

INSTRUCTIONS = """You are the Gardener. You tend the instruction files a developer's
coding agents read: CLAUDE.md files, AGENTS.md files, and skill files. You may change
nothing else.

From the recent reflections, find friction an instruction could prevent, and propose
at most 3 small, specific changes. Read a file before you change it. To change a
file, call propose_change with text that appears exactly once in it (old_text) and
its replacement (new_text); use an empty old_text only to create a new file. Never
put secrets, email addresses or absolute paths in a change.

Propose a change only if it passes every one of these:
- Recurring: the same problem shows in the reflections of at least two different
  days. Your evidence quotes name those days. Something that happened once is not
  a rule yet; wait for it to happen again.
- General: guidance that holds across cases, not the procedure for one situation.
  A problem best handled case by case needs no instruction.
- Not about wisen itself: friction in wisen's own pipeline (its reflection runs,
  its synthesis sessions, its proposals) is a wisen bug, not an instruction for a
  coding agent. Propose nothing for it.
- In the right harness: a rule about one harness's behaviour (for example Claude
  Code's permission prompts) goes in that harness's own instruction file (Claude
  Code reads CLAUDE.md files; Codex and opencode read AGENTS.md files), never in
  another harness's file or repo.
- Owned by the right people: the owner's personal workflow (how they supervise,
  review, merge, run their own tooling) goes in their own files: their home
  CLAUDE.md, or the CLAUDE.md or AGENTS.md of their personal workspace. A shared
  team repo gets only knowledge about that repo that every contributor needs.
- Loaded at the right time: a CLAUDE.md or AGENTS.md is read on every turn of every
  session in its scope, so put a rule there only if every turn needs it, at the
  narrowest scope that covers where it applies (the home file for all of the
  owner's work in that harness; a workspace's or repo's own file for work there).
  A rule needed only for one kind of task goes in a skill for that task.
- Not already handled: call past_proposals first and compare content, not just the
  file. If the same or a similar change was proposed before, even for another file:
  one that was applied is in place, so propose it again only if the problem still
  shows in reflections dated after it was merged; one that was rejected comes with
  the owner's note, so drop the idea or propose it where the note points; never
  propose the same content at the same place again.

Learn from past decisions: a rejection note says what the owner did not want; a
measured "worse" says what made things worse. Do not propose again what was rejected
for the same reason. Every proposal waits for the owner's approval. If nothing passes,
propose nothing and say why in one sentence."""


class Evidence(BaseModel):
    """One quote from one day's reflection."""
    date: str    # the reflection's date, YYYY-MM-DD, as headed in the prompt
    quote: str


class Draft(BaseModel):
    """A pending `propose_change` call, turned into a proposal's content."""
    tool_call_id: str
    target: str
    diff: str
    base_blob: str | None
    rationale: str
    evidence: list[str]
    current: str | None
    proposed: str
    device_id: str | None = None   # drafted from this laptop's snapshot (server Gardener): it applies it


@dataclass
class Deps:
    repos: dict | None = None            # alias -> repo root; None: scan as the config connector does
    snapshot: SnapshotFiles | None = None   # the server's Gardener: uploaded files instead of repos
    open_targets: set[str] = field(default_factory=set)
    reflection_dates: set[str] = field(default_factory=set)   # the days the prompt showed; evidence names them
    history: list[dict] = field(default_factory=list)         # past proposals (wisen/gardener/loop.py past_outcomes)
    proposed: dict[tuple[str, str], dict] = field(default_factory=dict)   # every past change (loop.py proposed_changes)
    resuming: bool = False                # a run resumed with decisions: the calls are already checked
    drafts: dict[str, Draft] = field(default_factory=dict)
    branches: dict[str, str] = field(default_factory=dict)   # tool call id -> branch, when resuming

    def files(self) -> LocalFiles | SnapshotFiles:
        return self.snapshot if self.snapshot is not None else LocalFiles(self.repos)


def change_lines(diff: str) -> str:
    """What a diff adds and removes, without the headers and hunk positions, so
    two diffs of one change compare equal however the file moved around it."""
    return "\n".join(line for line in diff.splitlines()
                     if line[:1] in "+-" and not line.startswith(("+++", "---")))


def describe(p: dict) -> str:
    """One past proposal, for the model: what it was, and what became of it."""
    what = p["state"]
    if p["decision"] == "rejected":
        what += f"; the owner's note: {p['note'] or '(none)'}"
    if p["merged_at"]:
        what += f"; merged {p['merged_at'][:10]}"
    if p["verdict"]:
        what += f"; measured {p['verdict']}: {p['reason']}"
    lines = [f"- {p['target']} ({p['created_at'][:10]}): {what}",
             f"  rationale: {p['payload'].get('rationale', '')}"]
    lines += [f"  {line}" for line in change_lines(p["payload"].get("diff", "")).splitlines()]
    return "\n".join(lines)


def _draft(deps: Deps, tool_call_id: str, target: str, old_text: str, new_text: str, rationale: str,
           evidence: list[Evidence]) -> Draft:
    if len(evidence) > 5:
        raise ModelRetry("give at most 5 evidence quotes")
    dates = {e.date for e in evidence}
    if unknown := sorted(dates - deps.reflection_dates):
        raise ModelRetry(f"each evidence date must be the date of a reflection shown above; {unknown} is not")
    if len(dates) < 2:
        raise ModelRetry("the problem must show on at least two different days: quote reflections of two or "
                         "more dates, or do not propose this yet")
    quotes = [f"{e.date}: {e.quote}" for e in evidence]
    try:
        t = deps.files().open(target)
    except Refused as e:
        raise ModelRetry(str(e)) from None
    if t.name in deps.open_targets:
        raise ModelRetry(f"{t.name} already has a proposal waiting or being applied; leave it for now")
    if deps.snapshot is not None and (SECRET in new_text or EMAIL in new_text):
        # The server reads files secret-redacted; copying a placeholder
        # would write it over the real value.
        raise ModelRetry(f"new_text holds {SECRET} or {EMAIL}, a placeholder for text you cannot see; leave it out")
    current = t.current()
    if old_text:
        if current is None:
            raise ModelRetry(f"{t.name} does not exist; use an empty old_text to create it")
        if current.count(old_text) != 1:
            raise ModelRetry(f"old_text must appear exactly once in {t.name}; it appears {current.count(old_text)} times")
        proposed = current.replace(old_text, new_text, 1)
    else:
        if current is not None:
            raise ModelRetry(f"{t.name} exists; give the old_text to replace")
        proposed = new_text
    if proposed == current:
        raise ModelRetry("the change changes nothing")
    try:
        diff = t.diff(current, proposed)
    except GitError as e:
        raise ModelRetry(f"could not build the diff: {e}") from None
    if redact(diff) != diff:
        raise ModelRetry("the change holds a secret, an email address or an absolute path; leave it out")
    if p := deps.proposed.get((t.name, change_lines(diff))):
        raise ModelRetry(f"this same change to {t.name} was already proposed on {p['created_at'][:10]} "
                         f"({p['state']}); see past_proposals, and do not propose it again")
    if len(diff) > MAX_DIFF:
        raise ModelRetry(f"the change is too large (over {MAX_DIFF} characters as a diff); make it smaller")
    deps.open_targets.add(t.name)
    return Draft(tool_call_id=tool_call_id, target=t.name, diff=diff, base_blob=t.base_blob(current),
                 rationale=rationale, evidence=quotes, current=current, proposed=proposed, device_id=t.device_id)


def build_agent(model) -> Agent:
    agent = Agent(model, deps_type=Deps, output_type=[str, DeferredToolRequests], instructions=INSTRUCTIONS,
                  name="gardener")

    @agent.tool
    def list_files(ctx: RunContext[Deps]) -> list[str]:
        """Every file you may change, as <project>:<path>. "home" is the developer's
        home directory (their global CLAUDE.md and personal skills)."""
        return [f for f in ctx.deps.files().list() if f not in ctx.deps.open_targets]

    @agent.tool
    def past_proposals(ctx: RunContext[Deps]) -> str:
        """Every past proposal, newest first: its file, what became of it (the
        owner's note if rejected, the merge date if applied, the measured
        verdict), its rationale and the lines it added and removed."""
        return "\n".join(describe(p) for p in ctx.deps.history) or "No past proposals."

    @agent.tool
    def read_file(ctx: RunContext[Deps], target: str) -> str:
        """The current text of a file you may change, given as <project>:<path>."""
        try:
            text = ctx.deps.files().open(target).current()
        except Refused as e:
            raise ModelRetry(str(e)) from None
        if text is None:
            return f"{target} does not exist yet."
        return text if len(text) <= MAX_READ else text[:MAX_READ] + "\n[… cut]"

    def check(ctx: RunContext[Deps], target: str, old_text: str, new_text: str, rationale: str,
              evidence: list[Evidence]) -> None:
        if ctx.deps.resuming:
            return
        ctx.deps.drafts[ctx.tool_call_id] = _draft(ctx.deps, ctx.tool_call_id, target, old_text, new_text,
                                                   rationale, evidence)

    # Sent back for a refused target, one-day evidence or a bad date: a few
    # times per run, not once, before the run is given up.
    @agent.tool(requires_approval=True, args_validator=check, retries=3)
    def propose_change(ctx: RunContext[Deps], target: str, old_text: str, new_text: str, rationale: str,
                       evidence: list[Evidence]) -> str:
        """Propose one change to one file. The owner approves or rejects it later.

        Args:
            target: the file, as <project>:<path> from list_files.
            old_text: text that appears exactly once in the file; empty to create a new file.
            new_text: what replaces old_text.
            rationale: one or two sentences: the friction this prevents.
            evidence: 2 to 5 short quotes from the reflections that show the friction, each
                with the date of the reflection it comes from; at least two different dates.
        """
        branch = ctx.deps.branches.get(ctx.tool_call_id, "a wisen/gardener branch")
        return f"Approved. wisen commits this change on {branch}; the owner merges it when they choose."

    return agent
