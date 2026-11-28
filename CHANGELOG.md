# Changelog

All notable changes to wisen are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `wisen eval report` prints a shareable Markdown scorecard of a `paired`,
  `weekly` or `demo` report: the verdict, each gate check, per-cohort paired
  differences with 95% CIs, the largest per-case gains and regressions, and
  retention of earlier rules.
- `wisen eval demo` runs the paired protocol on a public, synthetic case bank
  (targeted, holdout and clean cohorts for six rules), simulated by default or
  for real with `--model`.
- Replay on a local model: `local:NAME` sends each case to an OpenAI-compatible
  server (such as `mlx_lm.server`) at `WISEN_LOCAL_BASE_URL`, with the
  instruction files as the system message. `local:NAME@URL` names a server per
  model, and `wisen eval demo --proposed-model … --rules …` compares two models.
- `wisen eval export-sft` and `wisen eval distill-demo` write mlx-lm training
  data from the case bank or for the demo rules, keeping holdout and frozen
  cases out; docs/training.md compares a LoRA with the same rules in the prompt.

- One Show proposals / Fold all proposals button hides the entire proposal list
  by default, keeping reflections near the top. The choice persists in the
  current tab; proposal notification links still open the right card.
- Dashboard proposal and pull-request cards fold, remembering their state in
  the current tab. Close card moves unwanted publish cards to the change log,
  with Reopen card to undo it; branches and GitHub pull requests stay untouched.

- A replay eval protocol for Gardener rules. `wisen eval freeze` pins the case
  and config versions; `paired` measures rule impact against a clean set;
  `weekly` checks retention and judge drift; `field` shows the promotion
  verdict alongside observational field evidence.
- Blind human labeling and failure analysis through `wisen eval`:
  `sample-replays`, `sample-turns`, `label-queue`, `label`, `calibrate`,
  `rejudge` and `failures`. See the [replay eval protocol](docs/replay-eval.md).

## [0.3.1] - 2026-11-23

### Changed

- New logo: a sprout, used in the README, the dashboard icons, the
  walkthrough's favicon, the social preview and the README walkthrough GIF.
- The social preview keeps every agent name on one line.
- The demo labels sample approval commands, and the quickstart shows how to read a saved reflection.

### Fixed

- `wisen serve` names the required extra instead of crashing when FastAPI is missing.
- `wisen doctor` reports errors loading API models and shows how to install missing dependencies.
- The Claude Code SessionStart hook works when the plugin path contains spaces.

## [0.3.0] - 2026-11-20

### Added

- `wisen demo` prints a sample reflection and Gardener proposal in a second,
  with no model call and nothing read from your machine.
- `wisen doctor` lists the agent sessions wisen finds per source, whether the
  reflection's and the Gardener's models can run (and the fix when they
  cannot), and the command to run next.

### Fixed

- Installing no longer warns that `pydantic-ai` has no `openai` or
  `anthropic` extra: the package already includes those providers.
- `WISEN_ROOT_DIR` set in `.env` now applies to every agent's history, not
  only to sources read after the model settings load.
- A JSON export whose top level is an array is skipped instead of aborting
  the read.
- `openai-chat:` and `openai-responses:` models report a missing
  `OPENAI_API_KEY` instead of failing at the first call.

### Documentation

- The README starts with `uvx wisen demo` and a three-command quickstart; the
  walkthrough is published on GitHub Pages instead of a file to download.

### Changed

- `wisen synthesize` with no date uses the latest day with sessions in the
  past 14 days when yesterday has none, instead of skipping, and says so.
  With none at all it points at `wisen doctor` and `wisen demo`. It now
  prints how many sessions it reads per source and how long the run took.
- Chat and the Gardener run on your Claude Pro/Max subscription through the
  Claude Agent SDK (`claude-sdk:sonnet`) when no model is configured and
  `OPENAI_API_KEY` is unset, so a subscription alone covers the reflection,
  chat and the Gardener. A configured API model works as before. Evals stay
  on the API model. They skip with one line only when neither a key nor a
  logged-in `claude` CLI is available, and the Gardener's replay judges each
  draft on the same model, so a subscription alone also covers it. A stage-B
  server's chat uses `WISEN_CHAT_MODEL` when it is set, and otherwise the
  Gardener's model (a configured API model and its key, else the
  subscription), but only while its mailbox holds a single account: with
  more, chat, the Gardener and synthesis refuse
  `claude-sdk:`/`claude-cli:` even when they are configured, and need API
  models.

## 0.2.1 - 2026-11-12

Never tagged or published to PyPI; its changes first ship in 0.3.0.

### Changed

- Synthesis runs on your Claude Pro/Max subscription by default
  (`claude-cli:sonnet`, via the Claude Code CLI). Chat and the Gardener keep
  using `WISEN_MODEL`, and without its API key they skip with one line naming
  the key instead of failing.

## [0.2.0] - 2026-11-11

First release published to PyPI (`pipx install wisen`).

### Added

- Claude Code plugin and marketplace: session-start hint, `/wisen:proposals`,
  `/wisen:reflect` and `/wisen:setup`.
- `wisen gardener reset` returns failed proposals to the inbox.
- Hover and keyboard tooltips on the dashboard's hill charts.

### Fixed

- Plugin setup verifies without running a synthesis and never overwrites an
  existing config.
- The Gardener runs git at the work tree, so the applier guard ignores the
  worker's current directory.
- The worker uploads the subscription usage packet again, so the dashboard
  panel comes back.

### Changed

- Tags publish to PyPI through trusted publishing and create a GitHub Release.
- CI runs the full test suite with pytest on Python 3.11 and 3.12; pre-commit
  and Dependabot are set up.
- Security policy, code of conduct, changelog, issue and PR templates, and a
  README that leads with a demo.

## [0.1.0] - 2026-10-29

First release.

### Added

- Connectors for Claude Code, Codex, Cursor, opencode, Gemini CLI, Pi, generic
  exports and git commits, read into one session shape.
- Daily reflection synthesis (Observer and Synthesizer): moods, friction
  points, one insight and recommendations; quiet days are skipped.
- Chat agent that searches reflections, transcripts and configs before it
  answers, served over SSE by `wisen serve`.
- The Gardener: proposes small edits to `CLAUDE.md`, `AGENTS.md` and skill
  files as git branches you approve, then measures them and proposes a revert
  if things got worse.
- Local SQLite store (`~/.wisen/wisen.db`), replacing MongoDB, and a durable
  SQLite job engine.
- Optional self-hosted server: dashboard, approval inbox, device pairing, Web
  Push and a Telegram bot, fed only through a typed egress gate that masks
  secrets and caps text length.
- Voice briefing through LiveKit (`[voice]` extra).
- Any pydantic-ai model; synthesis can run on a Claude subscription through
  `claude -p`.
- `wisen` CLI: `synthesize`, `serve`, `export`, `eval`, `egress` and more.

[Unreleased]: https://github.com/Vela3202019/wisen/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/Vela3202019/wisen/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Vela3202019/wisen/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Vela3202019/wisen/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Vela3202019/wisen/releases/tag/v0.1.0
