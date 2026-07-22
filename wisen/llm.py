"""
LLM model configuration, shared by the chat and synthesis agents.

Set WISEN_MODEL (env/.env) or [model].model in ~/.wisen/config.toml to switch
providers (pydantic-ai model strings):
  google:gemini-2.5-flash    (default — needs GEMINI_API_KEY)
  deepseek:deepseek-chat     (needs DEEPSEEK_API_KEY)
  anthropic:claude-opus-4-8  (needs ANTHROPIC_API_KEY)

Note: Claude Pro/Max subscription auth only works inside the Claude Code CLI —
it cannot back an API integration like this one. Anthropic access here
requires an API key.
"""
import os

from dotenv import load_dotenv

from wisen import config

load_dotenv()

DEFAULT_MODEL = "google:gemini-2.5-flash"


def get_model() -> str:
    model = config.get("model", "model", "WISEN_MODEL", DEFAULT_MODEL)
    if model.startswith("google:"):
        # pydantic-ai's Google provider reads GOOGLE_API_KEY; we document GEMINI_API_KEY
        os.environ.setdefault("GOOGLE_API_KEY", os.environ.get("GEMINI_API_KEY", ""))
    return model
