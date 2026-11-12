# Configuration

Copy [`config.example.toml`](../config.example.toml) to `~/.wisen/config.toml`; every key is documented
there. Environment variables (or a repo-root `.env`) always override the file.
The three that matter:

- **`synth_model`** (`WISEN_SYNTH_MODEL`): the nightly synthesis, the big
  token spend. Defaults to `"claude-cli:sonnet"`: your Claude Pro/Max
  subscription via the Claude Code CLI, at no extra cost (`claude` must be
  installed and logged in; `curl localhost:8000/health` shows `claude_cli`).
  wisen leaves `ANTHROPIC_API_KEY` out of the CLI's environment, so a key in
  your shell never moves it to API billing. Prefer an API key? Set `"api"` to
  run it on `model`.
- **`model`** (`WISEN_MODEL`): chat and Gardener model, any pydantic-ai model
  string. They use tools, which the Claude Code CLI does not serve, so set
  that provider's API key: `openai:*` needs `OPENAI_API_KEY`, `google:*`
  needs `GEMINI_API_KEY` or `GOOGLE_API_KEY`, `deepseek:*` needs
  `DEEPSEEK_API_KEY`, `anthropic:*` needs `ANTHROPIC_API_KEY`. Without that
  key the nightly synthesis still saves the reflection: the Gardener and chat
  skip with one line naming the key, and `wisen gardener run <date>` runs that
  day's Gardener once the key is set. Running them on the subscription is
  coming next.
- **`[paths]`**: where your agents' dot-dirs and git repos live. Point
  `root_dir` at a synced copy to reflect on another machine.
