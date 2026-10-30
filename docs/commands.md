# Commands and extras

## Commands

```bash
wisen synthesize [YYYY-MM-DD]   # ingest sessions + write one day's reflection, then the Gardener batch
                                #   (--force: run a day that has no sessions or is already reflected on)
wisen serve                     # POST /synthesize, GET /reflections, POST /chat (SSE)
wisen export [dir]              # dump reflections to JSON files
wisen egress                    # what passed the egress gate today
wisen eval <args>               # case bank: mine, list, review, metrics, replay, compare
wisen gardener list|approve|reject|status|run  # Gardener proposals when not paired
wisen worker                    # laptop daemon: syncs with a paired server (outbound only);
                                #   unpaired, runs local Gardener jobs only
wisen cloud pair https://<host> # pair this laptop with your wisen server
wisen cloud serve               # run the server itself (on your server)
wisen migrate-mongo             # one-off: copy a pre-SQLite MongoDB into SQLite
```

`scripts/` has thin wrappers for launchd or cron (`run_synthesis.py`,
`export_reflections.py`, `collect_usage.py`); they bootstrap `sys.path`
themselves and run under system Python.

## Extras

| Extra | Adds |
|---|---|
| `pip install -e ".[server]"` | FastAPI + uvicorn (`wisen serve`) |
| `pip install -e ".[cloud]"` | FastAPI + uvicorn + pywebpush (`wisen cloud serve`, on your server) |
| `pip install -e ".[voice]"` | LiveKit voice agent: the morning briefing, out loud |
| `pip install -e ".[dev]"` | pytest + ruff |
| `pip install -e ".[migrate]"` | pymongo, for the one-off `wisen migrate-mongo` |
