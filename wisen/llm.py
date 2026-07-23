"""
LLM model configuration, shared by the chat and synthesis agents.

Set WISEN_MODEL (env/.env) or [model].model in ~/.wisen/config.toml to switch
providers (pydantic-ai model strings):
  openai:gpt-5.6-terra         (default — needs OPENAI_API_KEY)
  deepseek:deepseek-v4-flash   (needs DEEPSEEK_API_KEY; OpenAI-compatible API)
  google:gemini-2.5-flash      (needs GEMINI_API_KEY)
  anthropic:claude-sonnet-5    (needs ANTHROPIC_API_KEY)
Any other provider pydantic-ai supports works the same way — pass its model
string and set that provider's API key env var.

Synthesis uses the same model by default; set WISEN_SYNTH_MODEL=claude-cli:<model>
to run it on a Claude subscription via the Claude Code CLI — see synthesis/claude_cli.py.

Note: Claude Pro/Max subscription auth only works inside the Claude Code CLI —
it cannot back an API integration like this one. Anthropic access here
requires an API key.
"""
import os

from dotenv import load_dotenv

from wisen import config

load_dotenv()

DEFAULT_MODEL = "openai:gpt-5.6-terra"


def get_model() -> str:
    model = config.get("model", "model", "WISEN_MODEL", DEFAULT_MODEL)
    if model.startswith("google:"):
        # pydantic-ai's Google provider reads GOOGLE_API_KEY; we document GEMINI_API_KEY
        os.environ.setdefault("GOOGLE_API_KEY", os.environ.get("GEMINI_API_KEY", ""))
    return model
