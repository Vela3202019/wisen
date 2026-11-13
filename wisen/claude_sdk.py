"""
Chat and the Gardener on the developer's own Claude subscription, through the
Claude Agent SDK (WISEN_MODEL=claude-sdk:sonnet; the default when no model is
configured and OPENAI_API_KEY is unset — see llm.py).

The SDK drives the `claude` CLI the developer is logged in to, so it runs on
their subscription login, never on ANTHROPIC_API_KEY: the SDK merges `env`
over os.environ, so the empty value here blanks a key from the shell (synthesis
removes the key from the CLI's environment instead). Anthropic's policy on
subscription login in
Agent SDK products: https://code.claude.com/docs/en/agent-sdk/overview — here
it is the developer's own subscription, on their own machine or server, over
their own history; an API key is the alternative.

The SDK serves one model turn, and pydantic-ai keeps running the agent: each
model request becomes one SDK query in which the agent's pydantic-ai tools are
offered as in-process MCP tools under the same names and JSON schemas, with
max_turns=1. The tool calls the model makes come back to pydantic-ai as
ToolCallParts, and pydantic-ai runs the real tools, so argument validation,
ModelRetry, the Gardener's requires_approval deferral and its stored,
resumable conversation all work as on an API model. The MCP handlers are
stubs: the CLI calls them once before it stops at max_turns, and they do
nothing. The conversation so far is sent as a transcript in the prompt.

The CLI reports the turn's token usage in its result message. A non-streamed
turn reports those counts to pydantic-ai instead of letting pydantic-ai
estimate them, and a turn whose result carries none reports
`unmeasured_requests`, so a count built from it stays an honest lower bound
rather than a made-up number (the Gardener's replay judge, the one place a
token count is shown, runs non-streamed).
"""
import json
import shutil
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)
from claude_agent_sdk.types import StreamEvent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RequestUsage

from wisen import config
from wisen.llm import SDK_PREFIX

SERVER = "wisen"
_TOOL_PREFIX = f"mcp__{SERVER}__"

_TRANSCRIPT_NOTE = """\
The conversation so far is in the user message, as a transcript: <user> turns, your \
earlier <assistant> replies, and your earlier <tool_call>s with their <tool_result>s. \
Continue it as the assistant: call a tool when you need one, or write your reply. \
Never write transcript tags or tool calls as text."""


def _text(content) -> str:
    return content if isinstance(content, str) else "\n".join(c for c in content if isinstance(c, str))


def system_prompt(messages: list[ModelMessage], info: AgentInfo) -> str:
    """The agent's system prompt and instructions, and how to read the transcript."""
    parts = [p.content for m in messages if isinstance(m, ModelRequest)
             for p in m.parts if isinstance(p, SystemPromptPart)]
    if info.instructions:
        parts.append(info.instructions)
    return "\n\n".join([*parts, _TRANSCRIPT_NOTE])


def transcript(messages: list[ModelMessage]) -> str:
    """The conversation so far as one prompt: what the user said, what the
    model answered or called, and what each tool returned."""
    out = []
    for m in messages:
        for p in m.parts:
            if isinstance(p, UserPromptPart):
                out.append(f"<user>\n{_text(p.content)}\n</user>")
            elif isinstance(p, TextPart):
                out.append(f"<assistant>\n{p.content}\n</assistant>")
            elif isinstance(p, ToolCallPart):
                out.append(f'<tool_call name="{p.tool_name}" id="{p.tool_call_id}">{p.args_as_json_str()}</tool_call>')
            elif isinstance(p, ToolReturnPart):
                out.append(f'<tool_result name="{p.tool_name}" id="{p.tool_call_id}">\n'
                           f"{p.model_response_str()}\n</tool_result>")
            elif isinstance(p, RetryPromptPart):
                if p.tool_name:
                    out.append(f'<tool_result name="{p.tool_name}" id="{p.tool_call_id}" error="true">\n'
                               f"{p.model_response()}\n</tool_result>")
                else:
                    out.append(f"<user>\n{p.model_response()}\n</user>")
    return "\n\n".join(out)


def mcp_tools(tools: list[ToolDefinition]):
    """The agent's tools as in-process MCP tools: same names, descriptions and
    JSON schemas. Their handlers do nothing: pydantic-ai runs the real tool."""
    async def stub(args):
        return {"content": [{"type": "text", "text": "(run by wisen)"}]}

    return [tool(t.name, t.description or "", t.parameters_json_schema)(stub) for t in tools]


def options(model: str, messages: list[ModelMessage], info: AgentInfo, stream: bool) -> ClaudeAgentOptions:
    tools = [*info.function_tools, *info.output_tools]
    cwd = config.cli_cwd()
    cwd.mkdir(parents=True, exist_ok=True)
    return ClaudeAgentOptions(
        model=model,
        system_prompt=system_prompt(messages, info),
        tools=[],                       # no built-in tools: only the agent's own
        mcp_servers={SERVER: create_sdk_mcp_server(SERVER, tools=mcp_tools(tools))},
        strict_mcp_config=True,         # none of the developer's MCP servers
        allowed_tools=[_TOOL_PREFIX + t.name for t in tools],
        setting_sources=[],             # none of their settings, hooks or CLAUDE.md
        max_turns=1,
        cwd=cwd,                        # the Claude Code connector skips it: wisen never reflects on its own runs
        cli_path=shutil.which("claude"),   # the CLI the login check asks (claude_cli.cli_problem)
        env={"ANTHROPIC_API_KEY": ""},  # the SDK merges env over os.environ: this blanks the shell's key
        include_partial_messages=stream,
    )


async def _turn(model: str, messages: list[ModelMessage], info: AgentInfo, stream: bool) -> AsyncIterator:
    """One model turn: text (a str) and tool calls (ToolUseBlocks), in order.
    Text comes as deltas when streaming, as whole blocks otherwise. The turn's
    usage is its last item."""
    try:
        async with aclosing(query(prompt=transcript(messages), options=options(model, messages, info, stream))) as it:
            async for m in it:
                if isinstance(m, StreamEvent):
                    delta = m.event.get("delta", {}) if m.event.get("type") == "content_block_delta" else {}
                    if delta.get("type") == "text_delta":
                        yield delta["text"]
                elif isinstance(m, AssistantMessage):
                    for b in m.content:
                        if isinstance(b, ToolUseBlock):
                            yield b
                        elif isinstance(b, TextBlock) and not stream:
                            yield b.text
                elif isinstance(m, ResultMessage):
                    # error_max_turns is the stop after a turn that called tools
                    if m.is_error and m.subtype != "error_max_turns":
                        raise RuntimeError(f"claude-sdk: {m.result or m.subtype}")
                    yield _ReportedUsage(getattr(m, "usage", None))
                    return
    except ClaudeSDKError as e:
        raise RuntimeError(f"claude-sdk: {e}") from e


def _request_usage(raw: dict | None) -> RequestUsage:
    """The turn's usage as the CLI reported it, in pydantic-ai's convention: the
    CLI's `input_tokens` are the uncached ones, so the cached reads and writes
    are added to it (`input_tokens` is their inclusive parent bucket). A turn
    whose result carried none is marked `unmeasured_requests`, so pydantic-ai
    never fills in its own estimate for a number the owner would read."""
    if raw:
        cached = raw.get("cache_creation_input_tokens", 0) + raw.get("cache_read_input_tokens", 0)
        usage = RequestUsage(input_tokens=raw.get("input_tokens", 0) + cached,
                             output_tokens=raw.get("output_tokens", 0),
                             cache_write_tokens=raw.get("cache_creation_input_tokens", 0),
                             cache_read_tokens=raw.get("cache_read_input_tokens", 0))
        if usage.has_values():
            return usage
    return RequestUsage(unmeasured_requests=1)


@dataclass
class _ReportedUsage:
    """The turn's usage as the CLI's result message carried it, yielded last."""
    raw: dict | None


def _tool_name(b: ToolUseBlock) -> str:
    return b.name.removeprefix(_TOOL_PREFIX)


def sdk_model(name: str) -> FunctionModel:
    """The pydantic-ai model for `claude-sdk:<model>`, `<model>` being a Claude
    Code model alias (sonnet, opus, haiku) or a full model name."""
    model = name.removeprefix(SDK_PREFIX).strip()

    async def request(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        parts, reported = [], None
        async for item in _turn(model, messages, info, stream=False):
            if isinstance(item, _ReportedUsage):
                reported = item.raw
            else:
                parts.append(TextPart(item) if isinstance(item, str)
                             else ToolCallPart(_tool_name(item), item.input, tool_call_id=item.id))
        return ModelResponse(parts=parts, usage=_request_usage(reported))

    async def request_stream(messages: list[ModelMessage], info: AgentInfo):
        calls = 0
        empty = True
        async for item in _turn(model, messages, info, stream=True):
            if isinstance(item, _ReportedUsage):
                continue
            if isinstance(item, str):
                empty = False
                yield item
            else:
                calls += 1
                empty = False
                yield {calls: DeltaToolCall(name=_tool_name(item), json_args=json.dumps(item.input),
                                            tool_call_id=item.id)}
        if empty:
            yield ""   # a stream needs one item, even from an empty turn

    return FunctionModel(request, stream_function=request_stream, model_name=name)
