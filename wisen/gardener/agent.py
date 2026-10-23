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
"""
from dataclasses import dataclass, field

from pydantic import BaseModel
from pydantic_ai import Agent, DeferredToolRequests, ModelRetry, RunContext

from wisen.gardener.git import GitError, one_file_diff
from wisen.gardener.targets import Refused, editable_files, resolve
from wisen.redact import redact

MAX_DIFF = 20_000   # the proposal packet's cap on a diff (wisen/packets.py)
MAX_READ = 20_000

INSTRUCTIONS = """You are the Gardener. You tend the instruction files a developer's
coding agents read: CLAUDE.md files, AGENTS.md files, and skill files. You may change
nothing else.

From the recent reflections, find friction an instruction could prevent, and propose
at most 3 small, specific changes, each grounded in evidence from the reflections.
Read a file before you change it. To change a file, call propose_change with text
that appears exactly once in it (old_text) and its replacement (new_text); use an
empty old_text only to create a new file. Never put secrets, email addresses or
absolute paths in a change.

Learn from past decisions: a rejection note says what the owner did not want; a
measured "worse" says what made things worse. Do not propose again what was rejected
for the same reason. Every proposal waits for the owner's approval. If nothing is
worth changing, propose nothing and say why in one sentence."""


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


@dataclass
class Deps:
    repos: dict | None = None            # alias -> repo root; None: scan as the config connector does
    open_targets: set[str] = field(default_factory=set)
    resuming: bool = False                # a run resumed with decisions: the calls are already checked
    drafts: dict[str, Draft] = field(default_factory=dict)
    branches: dict[str, str] = field(default_factory=dict)   # tool call id -> branch, when resuming


def _draft(deps: Deps, tool_call_id: str, target: str, old_text: str, new_text: str, rationale: str,
           evidence: list[str]) -> Draft:
    try:
        t = resolve(target, deps.repos)
    except Refused as e:
        raise ModelRetry(str(e)) from None
    if t.name in deps.open_targets:
        raise ModelRetry(f"{t.name} already has a proposal waiting or being applied; leave it for now")
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
        diff = one_file_diff(t.repo, t.path, current, proposed)
    except GitError as e:
        raise ModelRetry(f"could not build the diff: {e}") from None
    if redact(diff) != diff:
        raise ModelRetry("the change holds a secret, an email address or an absolute path; leave it out")
    if len(diff) > MAX_DIFF:
        raise ModelRetry(f"the change is too large (over {MAX_DIFF} characters as a diff); make it smaller")
    if len(evidence) > 5:
        raise ModelRetry("give at most 5 evidence quotes")
    if t.is_home:
        # Drafted against the file on disk; the applier records that file on
        # main before it branches, so its blob is the base.
        base_blob = t.repo.out("hash-object", "--stdin", input=current) if current is not None else None
    else:
        base = t.base()
        base_blob = t.repo.blob(base, t.path) if base else None
    deps.open_targets.add(t.name)
    return Draft(tool_call_id=tool_call_id, target=t.name, diff=diff, base_blob=base_blob,
                 rationale=rationale, evidence=evidence, current=current, proposed=proposed)


def build_agent(model) -> Agent:
    agent = Agent(model, deps_type=Deps, output_type=[str, DeferredToolRequests], instructions=INSTRUCTIONS,
                  name="gardener")

    @agent.tool
    def list_files(ctx: RunContext[Deps]) -> list[str]:
        """Every file you may change, as <project>:<path>. "home" is the developer's
        home directory (their global CLAUDE.md and personal skills)."""
        return [f for f in editable_files(ctx.deps.repos) if f not in ctx.deps.open_targets]

    @agent.tool
    def read_file(ctx: RunContext[Deps], target: str) -> str:
        """The current text of a file you may change, given as <project>:<path>."""
        try:
            text = resolve(target, ctx.deps.repos).current()
        except Refused as e:
            raise ModelRetry(str(e)) from None
        if text is None:
            return f"{target} does not exist yet."
        return text if len(text) <= MAX_READ else text[:MAX_READ] + "\n[… cut]"

    def check(ctx: RunContext[Deps], target: str, old_text: str, new_text: str, rationale: str,
              evidence: list[str]) -> None:
        if ctx.deps.resuming:
            return
        ctx.deps.drafts[ctx.tool_call_id] = _draft(ctx.deps, ctx.tool_call_id, target, old_text, new_text,
                                                   rationale, evidence)

    @agent.tool(requires_approval=True, args_validator=check)
    def propose_change(ctx: RunContext[Deps], target: str, old_text: str, new_text: str, rationale: str,
                       evidence: list[str]) -> str:
        """Propose one change to one file. The owner approves or rejects it later.

        Args:
            target: the file, as <project>:<path> from list_files.
            old_text: text that appears exactly once in the file; empty to create a new file.
            new_text: what replaces old_text.
            rationale: one or two sentences: the friction this prevents.
            evidence: up to 5 short quotes from the reflections that show the friction.
        """
        branch = ctx.deps.branches.get(ctx.tool_call_id, "a wisen/gardener branch")
        return f"Approved. wisen commits this change on {branch}; the owner merges it when they choose."

    return agent
