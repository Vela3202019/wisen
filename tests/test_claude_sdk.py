"""
Chat and the Gardener on the Claude subscription through the Agent SDK: which
model they resolve to, and the tool bridge (wisen/claude_sdk.py), with the SDK's
`query` faked (no live CLI call).
Run: python3 -m tests.test_claude_sdk
"""
import asyncio
import json
import re
import tempfile
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock
from claude_agent_sdk.types import StreamEvent
from pydantic_ai import Agent, DeferredToolRequests, RunContext
from pydantic_ai.models.function import FunctionModel

from tests.test_claude_cli import _env, _no_config
from wisen import claude_sdk, llm
from wisen.synthesis import claude_cli


def _result(subtype="success", is_error=False, result=None, usage=None) -> ResultMessage:
    return ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error, num_turns=1,
                         session_id="s", result=result, usage=usage)


def _text_events(text: str) -> list[StreamEvent]:
    return [StreamEvent(uuid=str(i), session_id="s", event={
        "type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": chunk}})
        for i, chunk in enumerate(re.findall(r"\S+\s*", text))]


class FakeQuery:
    """Stands in for claude_agent_sdk.query: each call plays the next turn and
    records the prompt and options it was given."""

    def __init__(self, *turns: list):
        self.turns = list(turns)
        self.calls: list[tuple[str, object]] = []

    def __call__(self, *, prompt, options):
        self.calls.append((prompt, options))
        turn = self.turns.pop(0)

        async def play():
            for m in turn:
                yield m
            raise AssertionError("read past the turn's result")

        return play()


def _tool_turn(name: str, args: dict, call_id: str = "toolu_1") -> list:
    return [AssistantMessage(content=[ToolUseBlock(id=call_id, name=f"mcp__wisen__{name}", input=args)], model="m"),
            _result("error_max_turns", is_error=True)]


def _answer_turn(text: str, usage: dict | None = None) -> list:
    return [AssistantMessage(content=[TextBlock(text=text)], model="m"), _result(usage=usage)]


class _patched:
    def __init__(self, fake: FakeQuery, problem: str | None = None):
        self.fake, self.problem = fake, problem

    def __enter__(self):
        self.saved = claude_sdk.query, claude_cli.cli_problem
        claude_sdk.query, claude_cli.cli_problem = self.fake, (lambda: self.problem)

    def __exit__(self, *exc):
        claude_sdk.query, claude_cli.cli_problem = self.saved


# ── model resolution ──────────────────────────────────────────────────────────

def test_subscription_is_the_default_without_a_key():
    with _no_config(), _env(WISEN_MODEL=None, OPENAI_API_KEY=None):
        assert llm.agent_model_name() == "claude-sdk:sonnet"
        assert llm.get_model() == llm.DEFAULT_MODEL   # evals and API synthesis stay on the API model
    with _no_config(), _env(WISEN_MODEL=None, OPENAI_API_KEY="sk-openai-test"):
        assert llm.agent_model_name() == llm.DEFAULT_MODEL
    with _no_config(), _env(WISEN_MODEL="google:gemini-2.5-flash", OPENAI_API_KEY=None):
        assert llm.agent_model_name() == "google:gemini-2.5-flash"   # a configured model is kept
    with _no_config(), _env(WISEN_MODEL="claude-sdk:opus", OPENAI_API_KEY="sk-openai-test"):
        assert llm.agent_model_name() == "claude-sdk:opus"
    print("✓ model: the subscription when nothing is configured and OPENAI_API_KEY is unset; config wins")


def test_as_model():
    model = llm.as_model("claude-sdk:sonnet")
    assert isinstance(model, FunctionModel) and model.model_name == "claude-sdk:sonnet"
    assert llm.as_model("openai:gpt-5.6-terra") == "openai:gpt-5.6-terra"
    print("✓ model: claude-sdk:* builds the Agent SDK model; API model strings pass through")


def test_server_chat_model():
    with _no_config(), _env(WISEN_CHAT_MODEL=None):
        assert llm.chat_model() == "claude-sdk:sonnet"
    with _no_config(), _env(WISEN_CHAT_MODEL="deepseek-v4-flash"):
        assert llm.chat_model() == "deepseek:deepseek-v4-flash"
    print("✓ model: the server's chat is DeepSeek by WISEN_CHAT_MODEL, else the subscription")


def test_model_problem_for_the_subscription():
    real = claude_cli.cli_problem
    try:
        with _no_config(), _env(WISEN_MODEL=None, OPENAI_API_KEY=None):
            claude_cli.cli_problem = lambda: None
            assert llm.model_problem() is None
            for problem, says in ((claude_cli._NOT_LOGGED_IN, "not logged in"),
                                  (claude_cli._NOT_INSTALLED, "not installed"),
                                  (claude_cli._COULD_NOT_CHECK, "not answering")):
                claude_cli.cli_problem = lambda problem=problem: problem
                line = llm.model_problem()
                assert says in line and "claude auth login" in line or says == "not answering", line
                assert "OPENAI_API_KEY" in line and "\n" not in line, line
    finally:
        claude_cli.cli_problem = real
    print("✓ model: a missing or logged-out CLI is one line naming the login and the API-key way out")


def test_chat_skips_when_the_subscription_is_unusable():
    from wisen.chat import agent as chat

    async def stream() -> list[dict]:
        return [e async for e in chat.stream_chat([{"role": "user", "content": "what did I ship?"}])]

    with _no_config(), _env(WISEN_MODEL=None, OPENAI_API_KEY=None), _patched(FakeQuery(), claude_cli._NOT_LOGGED_IN):
        line = llm.model_problem()
        assert asyncio.run(stream()) == [{"type": "token", "data": line}]
    print("✓ chat: no key and a logged-out CLI answers with the one line")


def test_gardener_skips_when_the_subscription_is_unusable():
    from wisen.gardener import loop
    from wisen.jobs import JobEngine
    from wisen.store import SqliteStore

    day, before = "2026-10-19", "2026-10-18"
    rec = {"target": "claude-md", "action": "note the model switch", "reason": "it repeats"}

    async def garden():
        store = SqliteStore(Path(tempfile.mkdtemp(prefix="wisen-nosub-")) / "garden.db", "local")
        for d in (before, day):
            await store.upsert_reflection({"date": d, "recommendations": [rec], "friction_points": []})
        engine = JobEngine(store, loop.HANDLERS)
        return await engine.run_until_done((await engine.enqueue("garden", {"date": day})).id)

    with _no_config(), _env(WISEN_MODEL=None, OPENAI_API_KEY=None, WISEN_ROOT_DIR=tempfile.mkdtemp()), \
            _patched(FakeQuery(), claude_cli._NOT_INSTALLED):
        job = asyncio.run(garden())
        assert job.state == "succeeded", job.error
        assert job.result["needs_key"] and "not installed" in job.result["skipped"], job.result
    print("✓ gardener: no key and no CLI skips in one line; the day is gardened again later")


# ── the tool bridge ───────────────────────────────────────────────────────────

def _agent() -> tuple[Agent, list]:
    ran = []
    agent = Agent(claude_sdk.sdk_model("claude-sdk:sonnet"), system_prompt="You are Wisen.",
                  instructions="Answer from the tools.")

    @agent.tool
    def get_reflection(ctx: RunContext, date: str) -> dict:
        """One day's reflection."""
        ran.append(date)
        return {"date": date, "alignment_score": 73}

    return agent, ran


def test_tool_call_round_trip():
    """The model's tool call comes back through the SDK, pydantic-ai runs the
    real tool, and the next turn sees its result in the transcript."""
    agent, ran = _agent()
    fake = FakeQuery(_tool_turn("get_reflection", {"date": "2026-11-10"}), _answer_turn("It was 73."))
    with _patched(fake):
        result = agent.run_sync("How was 2026-11-10?")
    assert result.output == "It was 73." and ran == ["2026-11-10"]

    (prompt1, opts), (prompt2, _) = fake.calls
    assert prompt1 == "<user>\nHow was 2026-11-10?\n</user>"
    assert "You are Wisen." in opts.system_prompt and "Answer from the tools." in opts.system_prompt
    assert opts.tools == [] and opts.setting_sources == [] and opts.strict_mcp_config and opts.max_turns == 1
    assert opts.env == {"ANTHROPIC_API_KEY": ""}   # never the API: the subscription login
    assert opts.allowed_tools == ["mcp__wisen__get_reflection"] and opts.model == "sonnet"
    assert opts.mcp_servers["wisen"]["type"] == "sdk"
    assert '<tool_call name="get_reflection" id="toolu_1">{"date":"2026-11-10"}</tool_call>' in prompt2
    assert '<tool_result name="get_reflection" id="toolu_1">' in prompt2 and '"alignment_score":73' in prompt2
    print("✓ bridge: a tool call goes out through the SDK, runs in pydantic-ai, and its result comes back")


def test_a_key_in_the_shell_is_blanked_for_the_cli():
    """A developer with ANTHROPIC_API_KEY exported still runs on the
    subscription: the SDK merges options.env over os.environ, so the blanked key
    wins over the shell's (an empty copy would leave the real one in place)."""
    agent, _ = _agent()
    fake = FakeQuery(_answer_turn("ok"))
    with _env(ANTHROPIC_API_KEY="sk-ant-from-the-shell"), _patched(fake):
        assert agent.run_sync("hi").output == "ok"
    assert fake.calls[0][1].env["ANTHROPIC_API_KEY"] == "", fake.calls[0][1].env
    print("✓ bridge: a key in the shell is blanked for the CLI, so the turn runs on the subscription")


def test_the_cli_reports_the_turns_usage():
    """A turn's tokens are the CLI's own counts, not pydantic-ai's estimate:
    the Gardener's replay cost is read from here. The CLI's `input_tokens` are
    the uncached ones, so the cached reads and writes count as input too."""
    agent, _ = _agent()
    usage = {"input_tokens": 1_200, "output_tokens": 34, "cache_creation_input_tokens": 7,
             "cache_read_input_tokens": 900}
    fake = FakeQuery(_answer_turn("ok", usage))
    with _patched(fake):
        result = agent.run_sync("hi")
    assert result.output == "ok"
    turns = result.usage
    assert (turns.input_tokens, turns.output_tokens) == (2_107, 34), turns
    assert (turns.cache_write_tokens, turns.cache_read_tokens) == (7, 900), turns
    assert turns.total_tokens == 2_141 and turns.unmeasured_requests == 0, turns
    print("✓ bridge: a turn reports the CLI's own token usage, cached tokens included")


def test_a_turn_without_usage_is_unmeasured():
    """The CLI can report no usage: the turn then says so, instead of passing
    pydantic-ai's guess off as the turn's tokens."""
    agent, _ = _agent()
    fake = FakeQuery(_answer_turn("ok"))
    with _patched(fake):
        result = agent.run_sync("hi")
    assert result.output == "ok"
    assert result.usage.total_tokens == 0 and result.usage.unmeasured_requests == 1, result.usage
    print("✓ bridge: a turn with no usage from the CLI is marked unmeasured, never estimated")


def test_tools_keep_their_schema():
    from pydantic_ai.tools import ToolDefinition

    schema = {"type": "object", "properties": {"date": {"type": "string"}}, "required": ["date"]}
    [t] = claude_sdk.mcp_tools([ToolDefinition(name="get_reflection", description="One day's reflection.",
                                               parameters_json_schema=schema)])
    assert (t.name, t.description, t.input_schema) == ("get_reflection", "One day's reflection.", schema)
    # The handler is a stub: pydantic-ai runs the real tool.
    assert asyncio.run(t.handler({"date": "2026-11-10"}))["content"][0]["type"] == "text"
    print("✓ bridge: each tool keeps its name, description and JSON schema")


def test_retry_and_approval_are_pydantic_ais():
    """A tool the owner must approve still pauses the run (the Gardener's
    propose_change), and a bad argument goes back to the model as a retry."""
    agent = Agent(claude_sdk.sdk_model("claude-sdk:sonnet"), output_type=[str, DeferredToolRequests])

    @agent.tool_plain(requires_approval=True)
    def propose_change(target: str, count: int) -> str:
        """Propose one change."""
        return "approved"

    fake = FakeQuery(_tool_turn("propose_change", {"target": "home:CLAUDE.md", "count": "many"}, "toolu_bad"),
                     _tool_turn("propose_change", {"target": "home:CLAUDE.md", "count": 1}, "toolu_ok"))
    with _patched(fake):
        result = agent.run_sync("tend the files")
    assert isinstance(result.output, DeferredToolRequests), result.output
    assert [c.tool_call_id for c in result.output.approvals] == ["toolu_ok"]
    assert "valid integer" in fake.calls[1][0]   # the validation error reached the model
    print("✓ bridge: approval-gated tools pause the run; invalid arguments are retried")


def test_chat_streams_through_the_bridge():
    """stream_chat on the subscription: the tool's progress events, then the
    answer's text deltas."""
    from wisen.chat import agent as chat

    class Store:
        async def list_reflections(self, limit=7, before=None):
            return [{"date": "2026-11-10", "alignment_score": 73, "synthesis_context": "a calm day"}]

        async def get_reflection(self, date):
            return None

    stream_turn = [*_text_events("Your week was steady."), _result()]
    fake = FakeQuery(_tool_turn("list_reflections", {}), stream_turn)

    async def run() -> list[dict]:
        return [e async for e in chat.stream_chat([{"role": "user", "content": "how was my week?"}], store=Store())]

    chat._agents.clear()
    try:
        with _no_config(), _env(WISEN_MODEL=None, OPENAI_API_KEY=None), _patched(fake):
            events = asyncio.run(run())
    finally:
        chat._agents.clear()
    assert [e["type"] for e in events][:2] == ["tool_call", "tool_result"], events
    assert "".join(e["data"] for e in events if e["type"] == "token") == "Your week was steady."
    assert fake.calls[1][1].include_partial_messages
    print("✓ chat: a subscription turn streams tool progress and the answer")


def test_error_result_is_one_error():
    agent, _ = _agent()
    fake = FakeQuery([_result("error_during_execution", is_error=True, result="rate limited")])
    with _patched(fake):
        try:
            agent.run_sync("hi")
            raise AssertionError("an error result passed")
        except RuntimeError as e:
            assert "rate limited" in str(e), e
    print("✓ bridge: an error result fails the turn with the CLI's message")


def test_transcript_renders_every_part():
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        RetryPromptPart,
        TextPart,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )

    text = claude_sdk.transcript([
        ModelRequest(parts=[UserPromptPart("hi")]),
        ModelResponse(parts=[TextPart("let me look"), ToolCallPart("t", {"a": 1}, tool_call_id="c1")]),
        ModelRequest(parts=[ToolReturnPart("t", {"b": 2}, tool_call_id="c1"),
                            RetryPromptPart("say it plainly")]),
    ])
    assert text.split("\n\n")[0] == "<user>\nhi\n</user>"
    assert "<assistant>\nlet me look\n</assistant>" in text
    assert '<tool_call name="t" id="c1">{"a":1}</tool_call>' in text
    assert json.loads(text.split('<tool_result name="t" id="c1">\n')[1].split("\n</tool_result>")[0]) == {"b": 2}
    assert "say it plainly" in text.split("<user>")[-1]
    print("✓ bridge: the transcript carries user turns, replies, tool calls, results and retries")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nall claude-sdk tests passed")
