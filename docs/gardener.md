# The Gardener

The Gardener tends the instructions your coding agents read. After a day's
synthesis it reads recent reflections and friction, and proposes small changes
to `CLAUDE.md` files, `AGENTS.md` files and skill files. Nothing else: no repo
code, no settings, no MCP config. The allow-list is a path check in the
applier, not a line in a prompt, so a proposal naming any other path is
refused even after approval.

What it proposes is bounded by its instructions and, where the code can
see it, by a check on each draft:

- **Recurring.** The problem must show in the reflections of at least two
  different days. Each evidence quote names the day it comes from, and a draft
  with quotes from one day is sent back; one reflection alone makes no run.
- **General.** Guidance that holds across cases, not a procedure for one
  situation.
- **In the right file.** The owner's personal workflow goes in their own
  files, never in a shared team repo; a rule about one harness's behaviour
  goes in that harness's instruction file; a rule needed on every turn goes
  in a `CLAUDE.md`/`AGENTS.md` at the narrowest scope that covers it, and a
  rule for one kind of task goes in a skill.
- **Not about wisen itself.** Friction in wisen's own pipeline is a wisen bug,
  not an instruction for a coding agent.
- **Not already handled.** The run reads every past proposal with its content
  (the `past_proposals` tool): one applied is in place unless the problem
  still recurs after its merge; one rejected comes with your note, so the idea
  is dropped or moved where the note points; the same change to the same file
  is refused by the code.

The loop is propose, approve, apply as a branch, measure weeks later:

1. A proposal carries the diff, a rationale and dated evidence quotes from
   at least two days. It reaches you in the dashboard's approval inbox when
   paired, or through `wisen gardener list` when not.
2. You approve, reject with a note (the note reaches the model on the next
   run), or let it expire after 7 days.
3. An approval becomes a git branch (`wisen/gardener/<proposal id>`) on your
   laptop. With a server, the approved card then shows the branch's diff and
   a **Push & create PR** button: the laptop pushes that one branch to the
   repo's remote and opens a pull request with the `gh` CLI (title, rationale
   and evidence in the description), and the card shows the link. wisen never
   pushes to, or merges into, a default branch; merging is yours.
4. Two weeks after a merge, the eval loop compares field metrics before and
   after. A change that made things worse gets a revert proposal.

Files outside any repo, such as `~/.claude/CLAUDE.md` and personal skills,
are tracked in a git repo wisen keeps for them (`~/.wisen/gardener/home.git`),
so a proposal there is a branch you merge like any other; it has no remote,
so there is no pull request for it. The design is in
[plan/cloud-architecture.md](plan/cloud-architecture.md) §5 and the
measurement side in [plan/eval-loop.md](plan/eval-loop.md).
