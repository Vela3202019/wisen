---
name: proposals
description: Review, approve, or reject wisen's Gardener proposals (one-line changes to CLAUDE.md, AGENTS.md, or skill files mined from your own corrections). Use when the user asks to see wisen proposals, what the Gardener suggested, or to approve or reject one.
---

# wisen proposals

The Gardener proposes small changes to the instruction files your agents read,
each backed by corrections mined from your real sessions. This skill is the
approval inbox, in chat.

## List

Run:

```
wisen gardener list --json
```

For each proposal in state `proposed`, show the user:

- the id (first 8 characters are enough to refer to it)
- the target file
- the rationale, verbatim
- the diff, in a fenced `diff` block

For a proposal in state `applied`, show the branch name and the merge command
from `wisen gardener list` (plain output); wisen never merges on its own.

If the list is empty, say so in one line and stop.

## Decide

Only act on an explicit instruction from the user naming a proposal.

- Approve: `wisen gardener approve <id>`. The branch is created at once; tell the user its name and that merging is theirs to do.
- Reject: `wisen gardener reject <id> --note "<the user's reason, in their words>"`. The note reaches the Gardener on its next run, so keep it verbatim.

Never approve or reject a proposal the user did not name. Never run `git merge` on a Gardener branch.

## When wisen is paired with a server

`approve` and `reject` refuse with a message naming the server. Decisions then
belong in the server's dashboard. Relay that message and stop.

## If wisen is not installed

`wisen: command not found` means the CLI is missing. Point the user to `/wisen:setup`.
