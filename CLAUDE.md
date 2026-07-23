# Wisen API

FastAPI backend that ingests coding-agent session history (Claude Code, Codex, Gemini CLI, Cursor, opencode, Pi), synthesizes daily "reflections", and answers questions about them via a chat agent.

## Run

```bash
brew services start mongodb-community            # local MongoDB (wisen db), auto-starts at login
pip install -e ".[server,dev]"                   # editable install; `wisen` CLI entry point
wisen serve --reload                             # API on :8000 (or: uvicorn wisen.main:app --reload)
```

Tests live in `tests/` and run as scripts from the repo root: `python3 -m tests.test_search` (also `test_ingest`, `test_sources`, `test_synthesis`, `test_evals`).

## Model config

`wisen/llm.py` resolves the pydantic-ai model string from `WISEN_MODEL` (env/`.env`) or `[model].model` in `~/.wisen/config.toml` (default `openai:gpt-5.6-terra`). Supported: `openai:*` (OPENAI_API_KEY), `google:*` (GEMINI_API_KEY), `deepseek:*` (DEEPSEEK_API_KEY), `anthropic:*` (ANTHROPIC_API_KEY). Both the chat and synthesis agents share it. `WISEN_SYNTH_MODEL=claude-cli:<model>` opts synthesis into `claude -p` on a Claude subscription (default: same model as chat). Env vars always override the config file (`wisen/config.py`).

## Layout

All package code lives under `wisen/`:

- `wisen/main.py` — FastAPI app: `/synthesize` (background job), `/reflections`, `/chat` (SSE)
- `wisen/cli.py` — `wisen` entry point: `synthesize`, `serve`, `export`, `eval <args>`
- `wisen/ingestion/` — pulls sessions from each agent's on-disk history
- `wisen/connectors/` — per-agent parsers + `agent_configs.py` (harness/config file enumeration)
- `wisen/synthesis/` — daily reflection synthesis agent
- `wisen/chat/` — chat agent (`agent.py`) + history/config search tools (`search.py`)
- `wisen/voice/` — LiveKit token endpoint (`[voice]` extra; lazily imported)
- `wisen/dashboard/` — stdlib-only web dashboard for remote hosting (synced logs + queued reflection triggers); `scripts/export_reflections.py` produces its data files
- `scripts/` — thin wrappers for launchd jobs; must stay runnable by system python (they bootstrap `sys.path` themselves)

## Conventions

- Search tools are grep-first (find candidate files with `grep -ril`, then parse) — keep new sources on that pattern for speed.
- Session sources live under the user's home dir (`~/.claude/projects`, `~/.codex/sessions`, etc.); connectors must tolerate missing dirs and malformed lines silently.
