"""
Structured-output LLM call routed through the Claude Code CLI (`claude -p`).

Lets synthesis run on a Claude subscription model with no ANTHROPIC_API_KEY and no
per-token API billing — subscription auth only works inside the CLI, not through
pydantic-ai (see llm.py). Opt in with WISEN_SYNTH_MODEL=claude-cli:<model> (requires
the `claude` CLI installed and logged in); by default synthesis runs on [model].model
like everything else. Only synthesis reads this var; chat is unaffected.

`claude -p` is an agent, not a single-shot structured-output API: run inside the project
it loads MCP servers/skills/tools and wanders instead of answering. So we run it in a
neutral cwd of wisen's own (`config.cli_cwd()`, which the Claude Code connector skips, so
a run never shows up as a session to reflect on) with MCP and agentic tools disabled,
embed the JSON Schema in the prompt, ask for a bare JSON object, and validate the text
result into the pydantic model ourselves.
"""
import json
import os
import re
import subprocess
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from wisen import config

T = TypeVar("T", bound=BaseModel)

CLI_PREFIX = "claude-cli:"
# Default: synthesis runs on the same API model as chat ([model].model). Set
# WISEN_SYNTH_MODEL=claude-cli:<model> to route it through a Claude subscription.
DEFAULT_SYNTH_MODEL = "api"
_TIMEOUT_S = 300
# Disable agentic tools so the model answers directly instead of exploring.
_DISALLOWED = (
    "Bash,Read,Write,Edit,Glob,Grep,Task,WebFetch,WebSearch,Skill,ToolSearch,"
    "TodoWrite,ReadMcpResourceTool,ListMcpResourcesTool,NotebookEdit,AskUserQuestion"
)


def _synth_model() -> str:
    return config.get("model", "synth_model", "WISEN_SYNTH_MODEL", DEFAULT_SYNTH_MODEL)


def is_cli_model() -> bool:
    return _synth_model().startswith(CLI_PREFIX)


def _model_name() -> str:
    return _synth_model().split(CLI_PREFIX, 1)[-1].strip()


def _extract_json_object(text: str) -> str:
    """Return the outermost {...} JSON object from a model text response."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("no JSON object in claude -p result")
    return m.group(0)


def _parse_result(stdout: str, output_type: type[T]) -> T:
    """Extract the model's text result from a `claude -p --output-format json` payload
    and validate its JSON body into output_type. Raises on missing/invalid output."""
    data = json.loads(stdout)
    events = data if isinstance(data, list) else [data]
    result_text = next(
        (e.get("result") for e in events
         if isinstance(e, dict) and e.get("type") == "result"),
        None,
    )
    if not result_text:
        raise ValueError("no result text in claude -p response")
    return output_type.model_validate_json(_extract_json_object(result_text))


def run_structured(system_prompt: str, user_prompt: str, output_type: type[T]) -> T:
    """Run one schema-constrained generation via `claude -p` on the subscription."""
    schema = json.dumps(output_type.model_json_schema())
    sys_prompt = (
        f"{system_prompt}\n\n"
        "Return ONLY a single JSON object that validates against this JSON Schema. "
        "No markdown fences, no commentary — just the raw JSON object.\n\n"
        f"JSON Schema:\n{schema}"
    )
    cmd = [
        "claude", "-p",
        "--model", _model_name(),
        "--append-system-prompt", sys_prompt,
        "--disallowedTools", _DISALLOWED,
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--output-format", "json",
    ]
    # Scrub the API key so the CLI always uses subscription auth, never API billing.
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    cwd = config.cli_cwd()
    cwd.mkdir(parents=True, exist_ok=True)

    last_err: Exception | None = None
    for _ in range(2):
        try:
            proc = subprocess.run(
                cmd, input=user_prompt, capture_output=True, text=True,
                env=env, timeout=_TIMEOUT_S, cwd=cwd,
                check=False,
            )
        except FileNotFoundError:
            raise SystemExit(
                "`claude` CLI not found but synthesis is set to claude-cli. "
                "Install Claude Code and log in, or unset WISEN_SYNTH_MODEL "
                "(or set it to \"api\") to synthesize on the chat model instead."
            )
        except subprocess.TimeoutExpired as e:
            last_err = e
            continue
        if proc.returncode != 0:
            last_err = RuntimeError(f"claude -p exited {proc.returncode}: {proc.stderr[:500]}")
            continue
        try:
            return _parse_result(proc.stdout, output_type)
        except (ValueError, ValidationError, json.JSONDecodeError) as e:
            last_err = e
    raise RuntimeError(f"claude -p structured call failed: {last_err}")
