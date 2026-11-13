# Contributing to wisen

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
Report security problems privately as [SECURITY.md](SECURITY.md) describes,
never in a public issue.

## Dev setup

```bash
git clone https://github.com/Vela3202019/wisen && cd wisen
pip install -e ".[server,cloud,dev]" # editable install + FastAPI + Web Push + pytest/ruff
```

You don't need API keys or any real agent history to work on ingestion —
the fixture test runs entirely offline:

```bash
python3 -m tests.test_fixtures       # committed sample sessions in tests/fixtures/
python3 -m tests.test_claude_cli     # claude -p adapter parsing (no live call)
python3 -m tests.test_claude_sdk     # Claude Agent SDK model + tool bridge (no live call)
python3 -m tests.test_egress         # cloud export leaks nothing off the allow-list
python3 -m tests.test_store          # SQLite store on a temp file
python3 -m tests.test_jobs           # job engine + proposal state machine
python3 -m tests.test_cloud          # server stage A, in-process, fake push + model
python3 -m tests.test_gardener       # Gardener loop on temp repos with a fake model
python3 -m tests.test_stage_b        # stage B: two laptops, the server reflects; fake models
python3 -m tests.test_telegram       # Telegram bot against a fake Bot API
```

CI runs `pytest tests` on Python 3.11 and 3.12, plus the pre-commit hooks
(`pre-commit run --all-files`). If those pass locally, you're green.

The other tests read *your* real local data and are useful once you actually
use wisen:

```bash
python3 -m tests.test_ingest         # your real session history (nothing written)
python3 -m tests.test_sources        # per-connector, real local data
python3 -m tests.test_search         # chat search tools over your real history
python3 -m tests.test_synthesis      # end-to-end: calls the LLM, writes SQLite
```

Tests are script-style (`python3 -m tests.<name>` from the repo root).
`tests/conftest.py` lets pytest run each script as one test. `pytest` also runs
`test_ingest` and `test_sources`, but skips `test_search` and `test_synthesis`.

## Adding a connector

Connectors are deliberately dumb: find files → parse tolerantly (skip anything
malformed, silently) → emit `NormalizedSession`.

1. Copy the shape of `wisen/connectors/claude_code.py` — each connector's
   docstring shows annotated examples of its raw on-disk format; document yours
   the same way.
2. Add the source name to the `Literal` in `wisen/ingestion/normalizer.py`.
3. Register it in `wisen/ingestion/pipeline.py`.
4. Add a small synthetic session file under `tests/fixtures/` (date
   `2026-01-15`) and an assertion in `tests/test_fixtures.py` so CI covers it.

A connector that dies takes down nothing — errors are collected, not raised.
Connectors must tolerate missing directories and malformed lines silently;
session sources live under the user's home dir and look different on every
machine.

## Conventions

- **Grep-first search**: search tools find candidate files with `grep -ril`
  first, then parse only those. Keep new sources on that pattern for speed.
- **Local-first**: raw transcripts are never written to the SQLite store — only the
  synthesized reflection. Don't add code paths that persist session content.
- **Lint**: `ruff check .` must pass.
- **Commits**: conventional commits — `<type>(<scope>): <subject>`, imperative
  mood, lowercase, ≤72 chars (`feat`, `fix`, `docs`, `refactor`, `test`,
  `chore`, …). One logical change per commit.
- **Changelog**: add a line under `Unreleased` in [CHANGELOG.md](CHANGELOG.md)
  for any user-facing change.

Releases: see [docs/releasing.md](docs/releasing.md) for the tag-to-PyPI pipeline.
