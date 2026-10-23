"""
The Gardener: wisen acting on what it learns, under human approval. It may
change only CLAUDE.md, AGENTS.md and skill files, always as a git branch you
merge (docs/plan/cloud-architecture.md §5).

  agent.py    the Gardener agent and its requires_approval tool
  targets.py  the allow-list, and where each file lives (wisen's home repo too)
  git.py      git plumbing that never touches your checkout
  applier.py  approved proposal -> one commit on one branch; merge detection
  loop.py     the durable jobs: garden, resume, apply, measure
"""
