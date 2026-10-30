---
name: setup
description: Install and configure the wisen CLI that this plugin drives (pip install, API key or Claude subscription, first synthesis). Use when wisen is not found, when the user asks to set up or install wisen, or when another wisen skill reports the CLI is missing.
---

# wisen setup

The plugin is a thin layer over the `wisen` CLI. Check, then install, then configure.

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

wisen needs one model for synthesis. Two choices, ask which:

- An API key: set `OPENAI_API_KEY` (or the key for the provider named in `WISEN_MODEL`).
- The user's Claude Pro/Max subscription, no API billing: set `WISEN_SYNTH_MODEL=claude-cli:claude-sonnet-5`. Requires the `claude` CLI to be installed and logged in, and `ANTHROPIC_API_KEY` must not be set, or Claude Code bills the API instead.

Optional: copy `config.example.toml` to `~/.wisen/config.toml` and point `[paths]` at where the agents' session directories live.

## 4. First run

```
wisen synthesize
```

reflects on yesterday. Then `/wisen:reflect` reads it and `/wisen:proposals` shows what the Gardener suggested.

Transcripts never leave the machine; see "What leaves your machine" in the README for what the model calls receive.
