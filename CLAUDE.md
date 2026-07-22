# Wisen API

FastAPI backend that ingests coding-agent session history (Claude Code, Codex, Gemini CLI, Cursor, opencode, Pi), synthesizes daily "reflections", and answers questions about them via a chat agent.

## Run

```bash
brew services start mongodb-community            # local MongoDB (wisen db), auto-starts at login
venv/bin/pip install -r requirements.txt
venv/bin/uvicorn main:app --reload                # API on :8000
```

Tests live in `tests/` and run as scripts from the repo root: `python3 -m tests.test_search` (also `test_ingest`, `test_sources`, `test_synthesis`, `test_evals`).

## Model config

`llm.py` resolves the pydantic-ai model string from `WISEN_MODEL` in `.env` (default `google:gemini-2.5-flash`). Supported: `google:*` (GEMINI_API_KEY), `deepseek:*` (DEEPSEEK_API_KEY), `anthropic:*` (ANTHROPIC_API_KEY). Both the chat and synthesis agents share it.

## Layout

- `main.py` — FastAPI app: `/synthesize` (background job), `/reflections`, `/chat` (SSE)
- `ingestion/` — pulls sessions from each agent's on-disk history
- `connectors/` — per-agent parsers + `agent_configs.py` (harness/config file enumeration)
- `synthesis/` — daily reflection synthesis agent
- `chat/` — chat agent (`agent.py`) + history/config search tools (`search.py`)
- `voice/` — LiveKit token endpoint
- `dashboard/` — stdlib-only web dashboard for remote hosting (synced logs + queued reflection triggers); `scripts/export_reflections.py` produces its data files

## Conventions

- Search tools are grep-first (find candidate files with `grep -ril`, then parse) — keep new sources on that pattern for speed.
- Session sources live under the user's home dir (`~/.claude/projects`, `~/.codex/sessions`, etc.); connectors must tolerate missing dirs and malformed lines silently.
