# wisen

A reflection harness for coding agents. It reads your real coding-agent sessions
(Claude Code, Codex, opencode, Cursor, Gemini CLI, Pi), synthesizes a nightly
"reflection" — mood, friction, alignment, recommendations — and is growing an eval
loop that measures whether those recommendations actually make the agents work
better for you.

The metaphor is deliberate: a two-stage pipeline modeled on sleep. An Observer
(NREM) extracts the emotionally charged moments from raw transcripts — rejections,
rephrase storms, hedges — and a Synthesizer (REM) finds cross-day patterns and
produces the morning briefing.

## Components

| Directory | What it does |
|---|---|
| `wisen/ingestion/` + `wisen/connectors/` | Read session history from each agent's on-disk format, plus agent configs (CLAUDE.md, MCP, skills) and git commits |
| `wisen/synthesis/` | Two-stage Observer → Synthesizer pipeline producing a `Reflection` per day (MongoDB) |
| `wisen/chat/` | Chat agent with grep-first search tools over session history and configs |
| `wisen/voice/` | LiveKit voice agent + token endpoint (morning briefing out loud) — `[voice]` extra |
| `wisen/dashboard/` | Stdlib-only web dashboard for a VPS: reflection history, subscription usage, remote reflection triggers |
| `wisen/evals/` | Correction case miner, outcome metrics, and headless replay — the ground-truth layer for evaluating config changes (see `docs/plan/eval-loop.md`) |
| `scripts/` | Thin wrappers for scheduled jobs (nightly synthesis, dashboard export, usage snapshot) |

## Install & run

```bash
brew services start mongodb-community      # local MongoDB (db: wisen)
pip install -e ".[server]"                 # editable install with the API server
wisen --help
```

```bash
wisen synthesize [YYYY-MM-DD]   # ingest sessions + write one day's reflection
wisen serve                     # API on :8000 — POST /synthesize, GET /reflections, POST /chat (SSE)
wisen export [dir]              # dump reflections to JSON files
wisen eval mine                 # see Evals below
```

Configuration: copy `config.example.toml` to `~/.wisen/config.toml` (model,
MongoDB, export paths). Environment variables / `.env` override the file —
`WISEN_MODEL` takes a pydantic-ai model string (default `google:gemini-2.5-flash`;
also `deepseek:*`, `anthropic:*`). Set `WISEN_SYNTH_MODEL=claude-cli:<model>` to run
synthesis through the Claude Code CLI on a Claude subscription instead of API billing.

## Evals

The reflection layer recommends config changes; the eval layer exists to stop that from
being a self-graded loop. Real user corrections (rejected tool calls, interruptions,
"no, that's not what I meant") are mined into a personal case bank with the
correction as ground truth:

```bash
wisen eval mine                  # scan transcripts → case bank (one JSON per case)
wisen eval list                  # inspect the bank
wisen eval review <id> --confirm|--reject
wisen eval metrics               # corrections/100 turns, friction half-life,
                                 # alignment-score-vs-reality correlation
wisen eval replay <id>           # re-run one case headlessly, judge the outcome
wisen eval compare --a A.md --b B.md   # run the suite under two config variants
```

The case bank stays local (gitignored) — it contains verbatim excerpts of your own
sessions. Full design in `docs/plan/eval-loop.md`.

## Tests

Script-style tests in `tests/`, run from the repo root:

```bash
python3 -m tests.test_evals      # synthetic, no side effects
python3 -m tests.test_sources    # reads real local session data
python3 -m tests.test_synthesis  # end-to-end: calls the LLM, writes MongoDB
```

## Deployment shape

Everything runs locally except the dashboard, which lives on a VPS. launchd jobs on
the Mac run nightly synthesis, export reflections + usage snapshots, and sync them to
the VPS; the dashboard queues manual reflection requests for the Mac to pick up when
awake.
