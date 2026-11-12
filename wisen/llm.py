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

Synthesis does not use this model by default: it runs on a Claude Pro/Max
subscription via the Claude Code CLI (WISEN_SYNTH_MODEL=claude-cli:sonnet) — see
synthesis/claude_cli.py. WISEN_SYNTH_MODEL=api runs it on this model instead.

A stage-B server's chat (wisen/cloud) runs on DeepSeek instead: WISEN_CHAT_MODEL
names the DeepSeek model, and DEEPSEEK_API_KEY is its key.

Note: Claude Pro/Max subscription auth only works inside the Claude Code CLI —
it cannot back the tool-using chat and Gardener agents built on this model.
Anthropic access here requires an API key. Without that key the Gardener and
chat skip with one line (`model_problem`) instead of failing.
"""
import os

from dotenv import load_dotenv

from wisen import config

load_dotenv()

DEFAULT_MODEL = "openai:gpt-5.6-terra"

# The key each pydantic-ai provider's model needs.
MODEL_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "google": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


def model_problem(model: str | None = None) -> str | None:
    """Why chat and the Gardener cannot run, as one line; None when the
    provider's API key is set."""
    name = model or get_model()
    provider = name.split(":", 1)[0]
    key = MODEL_KEY_ENV.get(provider)
    if key is None or os.getenv(key):
        return None
    return (f"Gardener and chat need an API key for {provider}: set {key} "
            "(or another WISEN_MODEL) to enable them")


def get_model() -> str:
    model = config.get("model", "model", "WISEN_MODEL", DEFAULT_MODEL)
    if model.startswith("google:"):
        # pydantic-ai's Google provider reads GOOGLE_API_KEY; we document GEMINI_API_KEY
        os.environ.setdefault("GOOGLE_API_KEY", os.environ.get("GEMINI_API_KEY", ""))
    return model


def chat_model() -> str:
    """The stage-B server's chat model: DeepSeek, named by WISEN_CHAT_MODEL."""
    name = config.get("model", "chat_model", "WISEN_CHAT_MODEL")
    if not name:
        raise RuntimeError("set WISEN_CHAT_MODEL to the DeepSeek model the server's chat runs on")
    return f"deepseek:{name}"
