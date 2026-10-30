# Development

## Project status

wisen is a personal project under active development; expect the schema and
CLI to move. What exists today and what is planned is written down in
`docs/plan/`:

- [cloud-architecture.md](plan/cloud-architecture.md): the server is
  built in both stages: stage A (local brain, server mailbox) by default, and
  stage B (the Observer on the laptop, the Synthesizer and the Gardener on
  the server) with `WISEN_CLOUD_STAGE=b`. Reflection chat on the server is
  an API (`POST /api/chat`); the dashboard does not call it yet.
- [eval-loop.md](plan/eval-loop.md): the case miner, counterfactual
  replay and the gate that measures whether a Gardener change helped.

## Upgrading from MongoDB

Upgrading from a MongoDB-backed wisen? Run `pip install -e ".[migrate]"` and
`wisen migrate-mongo` once while MongoDB is still running. It copies your
reflections and chat/voice sessions into the SQLite file. Re-running it is
safe: anything already in SQLite is skipped, never overwritten.

## Tests

Script-style tests, run from the repo root. `python3 -m tests.test_fixtures`
runs offline on committed sample sessions; the full list, what CI runs, and
the tests that read your real local data are in
[CONTRIBUTING.md](../CONTRIBUTING.md#dev-setup).

## Contributing

Dev setup, the offline fixture workflow, and a step-by-step guide to adding a
connector for a new coding agent live in [CONTRIBUTING.md](../CONTRIBUTING.md).
Connectors are deliberately dumb (find files, parse tolerantly, emit
`NormalizedSession`), so a first contribution is small.
