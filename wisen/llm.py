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

Chat and the Gardener (`agent_model_name`) run on this model too, or on the
developer's own Claude subscription through the Claude Agent SDK
(claude-sdk:sonnet; see claude_sdk.py). That is their default when no model is
configured and OPENAI_API_KEY is unset; WISEN_MODEL=claude-sdk:<model> picks it
explicitly. Evals stay on this API model.

A stage-B server's chat (wisen/cloud) runs on DeepSeek when WISEN_CHAT_MODEL
names the DeepSeek model (DEEPSEEK_API_KEY is its key), and on the
subscription when it is unset — but only while the server holds a single
account (the owner). With more, chat and the Gardener need an API model, since
one person's subscription must never answer for other people.

Without a usable model — no key for an API model, or the `claude` CLI missing
or logged out for the subscription — the Gardener and chat skip with one line
(`model_problem`) instead of failing.
"""
import os
from collections.abc import Callable

from dotenv import load_dotenv

from wisen import config

load_dotenv()

DEFAULT_MODEL = "openai:gpt-5.6-terra"
SDK_PREFIX = "claude-sdk:"
CLI_PREFIX = "claude-cli:"   # synthesis through `claude -p` (synthesis/claude_cli.py)
# `sonnet` is Claude Code's alias for its latest Sonnet.
DEFAULT_SDK_MODEL = "claude-sdk:sonnet"

# The keys each pydantic-ai provider's model accepts; any one of them is enough.
MODEL_KEY_ENV = {
    "openai": ("OPENAI_API_KEY",),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
}


def model_key_set(model: str) -> bool:
    """Whether a key this model's provider accepts is set."""
    return any(os.getenv(key) for key in MODEL_KEY_ENV.get(model.split(":", 1)[0], ()))


def is_subscription(model: str) -> bool:
    """Whether `model` runs on the developer's own Claude subscription — through
    the Agent SDK (chat, the Gardener) or the CLI (synthesis) — rather than on a
    provider API key."""
    return model.startswith((SDK_PREFIX, CLI_PREFIX))


_served_accounts: Callable[[], int] | None = None


def set_server_accounts(count: Callable[[], int] | None) -> None:
    """How many accounts this process serves: `wisen cloud serve` passes its
    mailbox's user count, and every other entry point leaves it unset (its own).
    More than one turns the subscription default off: one person's subscription
    must never answer for other people."""
    global _served_accounts
    _served_accounts = count


def many_accounts() -> bool:
    return _served_accounts is not None and _served_accounts() > 1


def model_problem(model: str | None = None) -> str | None:
    """Why chat and the Gardener cannot run, as one line; None when the
    provider's API key is set, or the subscription's `claude` CLI is
    installed and logged in."""
    name = model or agent_model_name()
    if name.startswith(SDK_PREFIX):
        from wisen.synthesis import claude_cli

        problem = claude_cli.cli_problem()
        if problem is None:
            return None
        state = {
            claude_cli._NOT_INSTALLED: "not installed: install Claude Code and run `claude auth login`",
            claude_cli._NOT_LOGGED_IN: "not logged in: run `claude auth login`",
        }.get(problem, "not answering `claude auth status --json`: check it is installed and logged in")
        return (f"Gardener and chat run on your Claude subscription through the `claude` CLI, which is {state}; "
                "or set OPENAI_API_KEY (or another WISEN_MODEL and its key) to use the API")
    if model_key_set(name):
        return None
    provider = name.split(":", 1)[0]
    keys = MODEL_KEY_ENV.get(provider)
    if keys is None:
        return None
    if many_accounts():
        return (f"Gardener and chat need an API key for {provider}: set {' or '.join(keys)} "
                "(this server serves more than one account, so it cannot use your Claude subscription)")
    return (f"Gardener and chat need an API key for {provider}: set {' or '.join(keys)} "
            f"(or another WISEN_MODEL; {DEFAULT_SDK_MODEL} runs them on your Claude subscription) to enable them")


def get_model() -> str:
    model = config.get("model", "model", "WISEN_MODEL", DEFAULT_MODEL)
    if model.startswith("google:"):
        # pydantic-ai's Google provider reads GOOGLE_API_KEY; we document GEMINI_API_KEY
        os.environ.setdefault("GOOGLE_API_KEY", os.environ.get("GEMINI_API_KEY", ""))
    return model


def eval_model() -> str:
    """The model evals run on: the configured model, which they need to be an
    API model. `wisen eval` and its miner judge on a key, never on the
    subscription (the Gardener's replay passes its own model instead)."""
    model = get_model()
    if is_subscription(model):
        raise SystemExit(f"evals need an API model, but WISEN_MODEL is {model}; "
                         "set an API model and its key (for example OPENAI_API_KEY)")
    return model


def agent_model_name() -> str:
    """The model chat and the Gardener run on: the configured model; with none
    configured, the default API model when its key is set, else the Claude
    subscription — which a server with more than one account never gets."""
    if config.get("model", "model", "WISEN_MODEL") or model_key_set(DEFAULT_MODEL) or many_accounts():
        return get_model()
    return DEFAULT_SDK_MODEL


def as_model(name: str):
    """A pydantic-ai model for `name`: the string itself, or for claude-sdk:*
    the Agent SDK-backed model."""
    if name.startswith(SDK_PREFIX):
        from wisen.claude_sdk import sdk_model

        return sdk_model(name)
    return name


def chat_model() -> str:
    """The stage-B server's chat model: DeepSeek, named by WISEN_CHAT_MODEL;
    unset, the Claude subscription for a single-account server, and the default
    API model for a server that serves more than one account."""
    name = config.get("model", "chat_model", "WISEN_CHAT_MODEL")
    if name:
        return f"deepseek:{name}"
    return get_model() if many_accounts() else DEFAULT_SDK_MODEL
