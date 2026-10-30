<div align="center">

<img src="docs/assets/logo.svg" width="112" height="112" alt="wisen logo: a loose line sketching a range of hills">

# wisen

**wisen reads your coding-agent sessions and turns each day into a private
reflection, a chat about your work, and better instruction files for your agents.**

[![CI](https://github.com/Vela3202019/wisen/actions/workflows/ci.yml/badge.svg)](https://github.com/Vela3202019/wisen/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE) [![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml) [![Built with pydantic-ai](https://img.shields.io/badge/built%20with-pydantic--ai-E92063.svg)](https://ai.pydantic.dev)

<a href="https://claude.com/claude-code" title="Claude Code"><img src="docs/assets/agents/claude-code.svg" width="28" height="28" alt="Claude Code"></a>
<a href="https://openai.com/codex/" title="Codex"><img src="docs/assets/agents/codex.svg" width="28" height="28" alt="Codex"></a>
<a href="https://cursor.com" title="Cursor"><img src="docs/assets/agents/cursor.svg" width="28" height="28" alt="Cursor"></a>
<a href="https://github.com/google-gemini/gemini-cli" title="Gemini CLI"><img src="docs/assets/agents/gemini-cli.svg" width="28" height="28" alt="Gemini CLI"></a>
<a href="https://opencode.ai" title="opencode"><img src="docs/assets/agents/opencode.svg" width="28" height="28" alt="opencode"></a>
<a href="https://pi.dev" title="Pi"><img src="docs/assets/agents/pi.svg" width="28" height="28" alt="Pi"></a>

</div>

---

You spend hours a day with coding agents: corrections, rephrases, rejected
tool calls, the occasional flow state. Then it all evaporates. wisen reads
those sessions where your agents already keep them, writes one *reflection*
for the day, and proposes small fixes to the instruction files your agents
read. You approve each fix, and later sessions show whether it helped.

## Quickstart

You need Python 3.11+ and one LLM API key. The default model is
`openai:gpt-5.6-terra`, so `OPENAI_API_KEY`; any pydantic-ai provider works
(see [Configuration](docs/configuration.md)).

```bash
git clone https://github.com/Vela3202019/wisen && cd wisen
pip install -e ".[server]"
export OPENAI_API_KEY=sk-...            # or put it in a repo-root .env
wisen synthesize                        # reflect on yesterday (one real LLM call)
wisen serve                             # API on :8000: reflections and chat
```

A real reflection, abridged:

```jsonc
// 2026-07-20.json
{
  "your_mood":  { "label": "deep-focus",
    "summary": "Determined execution — dispatched 4 bugs, wrote a design doc,
                reviewed 2 PRs, 5 commits across 5 hours with zero hesitation." },
  "agent_mood": { "label": "confident",
    "summary": "Took ownership without hedging — zero 'I think', zero
                clarifications across 3 sessions, incl. a 213-turn debug session." },
  "alignment_score": 0.85,
  "insight": "The 70% rephrase rate on your end didn't indicate friction — it
              was tight-ship compression the agent could follow.",
  "friction_points": [
    { "type": "unclear-prompt",
      "description": "150 rephrases across 213 turns. The agent handled it,
                      but this is a load pattern worth examining." } ],
  "recommendations": [
    { "target": "workflow",
      "action": "If you switch models mid-session again, note why in a
                 one-liner so the pattern becomes analyzable." } ]
}
```

Every command and install extra is in [docs/commands.md](docs/commands.md).

## Features

- **Eight sources.** Claude Code, Codex, Cursor, opencode, Gemini CLI, Pi,
  generic exports and git commits, read into one session shape.
- **One reflection a day.** Moods, friction points, one insight and
  recommendations. Days with nothing new are skipped.
- **Chat that investigates.** Ask "how was my week?" and it searches your
  reflections, transcripts and configs before answering
  ([docs/chat.md](docs/chat.md)).
- **The Gardener.** Proposes small changes to your `CLAUDE.md`, `AGENTS.md`
  and skill files. Each one is a git branch you approve and merge yourself
  ([docs/gardener.md](docs/gardener.md)).
- **Voice briefing.** Hear the morning reflection out loud (the `[voice]` extra).
- **Your own server, optional.** The dashboard on your phone, push
  notifications, an approval inbox and a Telegram bot. It never sees a
  transcript ([docs/server.md](docs/server.md)).
- **Any model, or your subscription.** Any pydantic-ai model string; synthesis
  can also run on a Claude Pro/Max subscription through the Claude Code CLI.
- **No database server.** One SQLite file (`~/.wisen/wisen.db`), created on
  first use.

## Claude Code plugin

wisen ships as a Claude Code plugin, so the loop lives inside the sessions it
learns from. Install the CLI first (see [Quickstart](#quickstart)), then:

```bash
claude plugin marketplace add Vela3202019/wisen
claude plugin install wisen@vela
```

What it adds:

- **At session start**, one line of context when the Gardener has proposals
  waiting or approved branches you have not merged. Nothing when there is
  nothing to say, and nothing when `wisen` is not installed.
- **`/wisen:proposals`**: the approval inbox in chat. Shows each proposal's
  target, rationale and diff; approves or rejects only the one you name.
- **`/wisen:reflect`**: runs or reads a day's reflection and summarises it.
- **`/wisen:setup`**: installs and configures the CLI on a machine that does
  not have it.

The plugin is the `plugin/` directory; the marketplace is `.claude-plugin/marketplace.json`
at the repo root. `claude plugin validate .` checks both.

## How it works

- **Read.** Connectors parse each agent's on-disk history into one shape and
  skip anything missing or malformed.
- **Observe.** The Observer reads the day's turns and picks out the charged
  moments: rejections, rephrase storms, hedges.
- **Reflect.** The Synthesizer turns those notes into the day's reflection,
  stored in SQLite.
- **Use it.** The chat agent, the dashboard and the voice briefing read the
  reflection; the Gardener turns recurring friction into proposals.
- **Measure.** Two weeks after you merge a proposal, the eval loop compares
  before and after, and proposes a revert if things got worse.

The full pipeline, with a diagram and a map of the code, is in
[docs/how-it-works.md](docs/how-it-works.md).

## Privacy

- **Stays on your machine:** raw transcripts, git history and agent configs
  (wisen does not copy them), the SQLite file and your API keys.
- **Goes to the model provider you pick:** excerpts of the day's sessions
  (each turn cut to 400 characters, and a turn can include tool output), up to 20 config
  files, commit messages, and chat search results. With `claude-cli:…`
  synthesis, that provider is Anthropic under your own account. Voice sends
  your speech to Google's Gemini.
- **Goes to your own server, if you pair one:** derived results, which can
  quote short pieces of your sessions; in stage B also the Observer's notes
  and the text of the files the Gardener may edit, never raw transcripts or
  tool output. Everything passes one egress gate
  that checks a typed schema, masks secrets and caps text length, and
  `wisen egress` shows what left today.

The exact limits, every packet type, stage B, Telegram and the full list of
network calls are in [docs/privacy.md](docs/privacy.md).

## Documentation

- [Configuration](docs/configuration.md): models, API keys, paths
- [Commands and extras](docs/commands.md)
- [How it works](docs/how-it-works.md)
- [Chat](docs/chat.md)
- [The Gardener](docs/gardener.md)
- [Your own server](docs/server.md), with setup details in [deploy/README.md](deploy/README.md)
- [What leaves your machine](docs/privacy.md)
- [Development](docs/development.md): project status, tests, upgrading from MongoDB

## Project status

A personal project under active development; expect the schema and CLI to
move. Plans live in [docs/plan/](docs/plan/).

## Contributing

Dev setup, offline tests and a guide to adding a connector for a new agent
are in [CONTRIBUTING.md](CONTRIBUTING.md). Connectors are small, so a first
contribution is too.

## License

[MIT](LICENSE)
