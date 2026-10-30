---
name: setup
description: Install and configure the wisen CLI that this plugin drives (pip install, API key or Claude subscription, a no-cost verification). Use when wisen is not found, when the user asks to set up or install wisen, or when another wisen skill reports the CLI is missing.
---

# wisen setup

The plugin is a thin layer over the `wisen` CLI. Check, install, configure, verify.
Setup never runs a synthesis: that is minutes of model calls and belongs in the
background, not in this session.

Use `wisen --help` and `~/.wisen/config.toml` as the sources of truth. Do not
read wisen's installed source files to work out its configuration.

## 1. Check

```
command -v wisen && wisen --help | head -3
```

If present, skip to step 3.

## 2. Install

Python 3.11+ is required.

```
pip install "wisen[server] @ git+https://github.com/Vela3202019/wisen"
```

Ask before running it. If the user prefers an editable checkout:

```
git clone https://github.com/Vela3202019/wisen && cd wisen && pip install -e ".[server]"
```

## 3. Configure

wisen needs one model for synthesis. Ask which:

- An API key: set `OPENAI_API_KEY` (or the key for the provider named in `WISEN_MODEL`).
- The user's Claude Pro/Max subscription, no API billing: `synth_model = "claude-cli:claude-sonnet-5"`
  under `[model]` in `~/.wisen/config.toml`. Requires the `claude` CLI installed and logged
  in. If `ANTHROPIC_API_KEY` is set in the shell, the Claude CLI bills the API instead, so
  run wisen with `env -u ANTHROPIC_API_KEY` or unset it for wisen's launchd job.

Editing `~/.wisen/config.toml`:

- If the file exists, read it first and change or add only the one key. Never replace the file.
- If it does not exist, copy `config.example.toml` from the repo, or create it with just the
  `[model]` section.

Show the user the resulting file.

## 4. Verify, without a model call

```
wisen gardener list
```

This reads the local database and prints open proposals, or nothing. It proves the install,
the config, and the database path in under a second. Stop here; setup is done.

## 5. Tell the user how synthesis runs

Say this, do not run it:

- `wisen synthesize` reflects on yesterday and takes one to several minutes of model calls.
  Run it in a separate terminal, or in the background, the first time.
- For every day after, the README's `scripts/run_synthesis.py` runs it from launchd or cron,
  and `wisen worker` runs the Gardener jobs.

Once a reflection exists, `/wisen:reflect` reads it and `/wisen:proposals` shows what the
Gardener suggested. Transcripts never leave the machine; see "What leaves your machine" in the README.
