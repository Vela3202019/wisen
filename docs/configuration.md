# Configuration

Copy [`config.example.toml`](../config.example.toml) to `~/.wisen/config.toml`; every key is documented
there. Environment variables (or a repo-root `.env`) always override the file.
The three that matter:

- **`synth_model`** (`WISEN_SYNTH_MODEL`): the nightly synthesis, the big
  token spend. Defaults to `"claude-cli:sonnet"`: your Claude Pro/Max
  subscription via the Claude Code CLI, at no extra cost (`claude` must be
  installed and logged in; `curl localhost:8000/health` shows `claude_cli`).
  wisen leaves `ANTHROPIC_API_KEY` out of the CLI's environment (and the Agent
  SDK path blanks it), so a key in your shell never moves it to API billing.
  Prefer an API key? Set `"api"` to run it on `model`, which must then be an
  API model.
- **`model`** (`WISEN_MODEL`): the chat and Gardener model. Unset, they run on
  your Claude Pro/Max subscription (`claude-sdk:sonnet`) unless
  `OPENAI_API_KEY` is set, which makes the default `openai:gpt-5.6-terra`.
  `claude-sdk:<model>` (for example `claude-sdk:opus`) picks the subscription
  explicitly. They use tools, so they go through the [Claude Agent
  SDK](https://code.claude.com/docs/en/agent-sdk/overview), which drives the same logged-in `claude` CLI as synthesis
  (`/health` shows `claude_cli`). `ANTHROPIC_API_KEY` is blanked in its
  environment (the Agent SDK merges its environment over yours), so a key in
  your shell never moves it to API billing. It is your own subscription, on
  your own server or machine, over your own history: a stage-B server whose
  mailbox holds more than one account refuses it for chat, the Gardener and
  synthesis (even when configured explicitly, `claude-sdk:` in `model` or
  `claude-cli:` in `synth_model`) and needs API models instead. Anthropic does
  not let third-party products offer claude.ai login unless it has approved
  them ([Agent SDK policy](https://code.claude.com/docs/en/agent-sdk/overview)).
  An API key is the alternative. Any other pydantic-ai model string runs on
  that provider's key: `openai:*` needs `OPENAI_API_KEY`, `google:*` needs
  `GEMINI_API_KEY` or `GOOGLE_API_KEY`, `deepseek:*` needs `DEEPSEEK_API_KEY`,
  `anthropic:*` needs `ANTHROPIC_API_KEY`. With neither that key nor a
  logged-in CLI, the nightly synthesis still saves the reflection. The
  Gardener and chat skip with one line saying what to set, and
  `wisen gardener run <date>` runs that day's Gardener once it is set. Evals
  (`wisen eval`) always use an API model.
- **`[paths]`**: where your agents' dot-dirs and git repos live. Point
  `root_dir` at a synced copy to reflect on another machine.
