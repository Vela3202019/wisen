# Changelog

All notable changes to wisen are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Claude Code plugin and marketplace: session-start hint, `/wisen:proposals`,
  `/wisen:reflect` and `/wisen:setup`.
- `wisen gardener reset` returns failed proposals to the inbox.
- Hover and keyboard tooltips on the dashboard's hill charts.

### Changed

- Synthesis runs on your Claude Pro/Max subscription by default
  (`claude-cli:sonnet`, via the Claude Code CLI). Chat and the Gardener keep
  using `WISEN_MODEL`, and without its API key they skip with one line naming
  the key instead of failing.

### Fixed

- Plugin setup verifies without running a synthesis and never overwrites an
  existing config.
- The Gardener runs git at the work tree, so the applier guard ignores the
  worker's current directory.
- The worker uploads the subscription usage packet again, so the dashboard
  panel comes back.

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

[Unreleased]: https://github.com/Vela3202019/wisen/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Vela3202019/wisen/releases/tag/v0.1.0
