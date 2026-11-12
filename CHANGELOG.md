# Changelog

All notable changes to wisen are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Chat and the Gardener run on your Claude Pro/Max subscription through the
  Claude Agent SDK (`claude-sdk:sonnet`) when no model is configured and
  `OPENAI_API_KEY` is unset, so a subscription alone covers the reflection,
  chat and the Gardener. A configured API model works as before. Evals stay
  on the API model. They skip with one line only when neither a key nor a
  logged-in `claude` CLI is available. A stage-B server's chat runs on the
  subscription when `WISEN_CHAT_MODEL` is unset.

## [0.2.1] - 2026-11-12

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

[Unreleased]: https://github.com/Vela3202019/wisen/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/Vela3202019/wisen/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Vela3202019/wisen/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Vela3202019/wisen/releases/tag/v0.1.0
