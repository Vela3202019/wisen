# Configuration

Copy [`config.example.toml`](../config.example.toml) to `~/.wisen/config.toml`; every key is documented
there. Environment variables (or a repo-root `.env`) always override the file.
The three that matter:

- **`model`** (`WISEN_MODEL`): chat model, any pydantic-ai model string. Set
  that provider's API key: `openai:*` needs `OPENAI_API_KEY`, `google:*`
  needs `GEMINI_API_KEY`, `deepseek:*` needs `DEEPSEEK_API_KEY`,
  `anthropic:*` needs `ANTHROPIC_API_KEY`.
- **`synth_model`** (`WISEN_SYNTH_MODEL`): the nightly synthesis, the big
  token spend. Defaults to `model`. Set `"claude-cli:claude-sonnet-5"` to run
  it on a Claude Pro/Max subscription via the Claude Code CLI instead, with
  no API billing (`claude` must be installed and logged in).
- **`[paths]`**: where your agents' dot-dirs and git repos live. Point
  `root_dir` at a synced copy to reflect on another machine.
