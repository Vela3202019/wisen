"""
`wisen doctor`: what wisen finds on this machine (agent sessions by source,
the models each stage would run on, and whether they can run) and the one
command to run next. Reads local history and asks `claude auth status`; makes
no model call.
"""
import asyncio

from pydantic_ai.exceptions import UserError
from pydantic_ai.models import infer_model

from wisen import __version__, config, llm
from wisen.ingestion.pipeline import session_days
from wisen.synthesis import claude_cli

SOURCES = ("claude-code", "codex", "cursor", "gemini-cli", "opencode", "pi", "export")


def _api_model_problem(model: str) -> str | None:
    try:
        infer_model(model)
    except (ImportError, UserError) as error:
        return str(error)
    return None


def _agent_model_problem(model: str) -> str | None:
    problem = llm.model_problem(model)
    if problem is not None or llm.is_subscription(model):
        return problem
    return _api_model_problem(model)


def synthesis_problem() -> tuple[str, str | None]:
    """The model the reflection runs on, and why it cannot run (None when it can)."""
    if claude_cli.is_cli_model():
        return claude_cli.synth_model(), claude_cli.cli_problem()
    model = llm.get_model()
    if llm.is_subscription(model):
        return model, (f'synth_model = "api" needs an API model, but WISEN_MODEL is {model}; set an API '
                       'model or drop synth_model = "api" to use the subscription')
    keys = llm.MODEL_KEY_ENV.get(model.split(":", 1)[0])
    if keys and not llm.model_key_set(model):
        return model, f"set {' or '.join(keys)} for {model}"
    return model, _api_model_problem(model)


async def report() -> tuple[str, bool]:
    """The doctor's report, and whether a reflection can run now."""
    days, errors = await session_days()
    totals = {source: 0 for source in SOURCES}
    last: dict[str, str] = {}
    for day, by_source in days.items():
        for source, n in by_source.items():
            totals[source] = totals.get(source, 0) + n
            last[source] = max(last.get(source, day), day)

    lines = [f"wisen {__version__}", "", f"Agent sessions under {config.root()}"]
    for source, n in totals.items():
        lines.append(f"  ✓ {source:<12} {n:>5} session{'s' if n != 1 else ' '}  latest {last[source]}" if n
                     else f"  · {source:<12}     none found")
    lines += [f"  ! {e}" for e in errors]

    synth, synth_problem = await asyncio.to_thread(synthesis_problem)
    agent = llm.agent_model_name()
    agent_problem = await asyncio.to_thread(_agent_model_problem, agent)
    lines += ["", "Models"]
    lines.append(f"  {'✓' if synth_problem is None else '✗'} reflection       {synth}")
    if synth_problem:
        lines.append(f"      {synth_problem}")
    lines.append(f"  {'✓' if agent_problem is None else '✗'} chat, Gardener   {agent}")
    if agent_problem:
        lines.append(f"      {agent_problem}")

    latest = max(days) if days else None
    lines += ["", "Next"]
    if latest is None:
        lines.append(f"  No sessions found. wisen reads the history Claude Code, Codex, Cursor, Gemini CLI, "
                     f"opencode and Pi keep under {config.root()}; set WISEN_ROOT_DIR if yours is elsewhere. "
                     "Meanwhile `wisen demo` shows a sample day.")
    elif synth_problem:
        lines.append(f"  Fix the reflection's model above, then: wisen synthesize {latest}")
    else:
        lines.append(f"  wisen synthesize {latest}   (one to a few minutes of model calls)")
    return "\n".join(lines), latest is not None and synth_problem is None


def main() -> int:
    text, ready = asyncio.run(report())
    print(text)
    return 0 if ready else 1
