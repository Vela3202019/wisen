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
| `ingestion/` + `connectors/` | Read session history from each agent's on-disk format, plus agent configs (CLAUDE.md, MCP, skills) and git commits |
| `synthesis/` | Two-stage Observer → Synthesizer pipeline producing a `Reflection` per day (MongoDB) |
| `chat/` | Chat agent with grep-first search tools over session history and configs |
| `voice/` | LiveKit voice agent + token endpoint (morning briefing out loud) |
| `dashboard/` | Stdlib-only web dashboard for a VPS: reflection history, subscription usage, remote reflection triggers |
| `evals/` | Correction case miner + outcome metrics — the ground-truth layer for evaluating config changes (see `docs/plan/eval-loop.md`) |
| `scripts/` | `run_synthesis.py` (nightly job), `export_reflections.py` (dashboard data), `collect_usage.py` (subscription usage snapshot) |

## Run

```bash
brew services start mongodb-community      # local MongoDB (db: wisen)
python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/uvicorn main:app --reload         # API on :8000
```

API surface: `POST /synthesize` (background job), `GET /reflections`, `POST /chat` (SSE),
plus the LiveKit token endpoint for voice.

Model config lives in `.env` — `WISEN_MODEL` takes a pydantic-ai model string
(default `google:gemini-2.5-flash`; also `deepseek:*`, `anthropic:*`). Note that
synthesis and evals bill against raw API keys, not a Claude subscription.

## Evals

The reflection layer recommends config changes; the eval layer exists to stop that from
being a self-graded loop. Real user corrections (rejected tool calls, interruptions,
"no, that's not what I meant") are mined into a personal case bank with the
correction as ground truth:

```bash
python3 -m evals mine            # scan transcripts → evals/cases/*.json
python3 -m evals list            # inspect the bank
python3 -m evals review <id> --confirm|--reject
python3 -m evals metrics         # corrections/100 turns, friction half-life,
                                 # alignment-score-vs-reality correlation
```

Phase 2 (planned): replay cases headlessly under config variants and gate CLAUDE.md
changes on measured improvement. Full design in `docs/plan/eval-loop.md`.

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
