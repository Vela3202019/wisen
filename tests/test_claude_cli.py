"""
Test the claude -p structured-output adapter's parsing/validation (no live CLI call).
Run: python3 -m tests.test_claude_cli
"""
import asyncio
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from wisen import config
from wisen.jobs import PermanentFailure
from wisen.llm import model_problem
from wisen.synthesis import claude_cli
from wisen.synthesis.claude_cli import _extract_json_object, _model_name, _parse_result
from wisen.synthesis.schema import DayObservation


@contextmanager
def _env(**values):
    """Set these env vars for the block (None removes them), then put back what
    was there: these tests run in-process, and a developer's shell exports
    leak into every later test's subprocesses."""
    saved = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def _no_config():
    """The built-in defaults, not this developer's ~/.wisen/config.toml."""
    saved = config.CONFIG_PATH
    config.CONFIG_PATH = Path("/nonexistent/wisen-config.toml")
    config._config.cache_clear()
    try:
        yield
    finally:
        config.CONFIG_PATH = saved
        config._config.cache_clear()


def _valid_observation() -> dict:
    return {
        "mood_signals_user": "terse, directive",
        "mood_signals_agent": "hedging early, confident later",
        "topics_raw": ["synthesis", "cli adapter"],
        "key_moments": [
            {"session_source": "claude-code", "quote": "go", "significance": "approval"}
        ],
        "commit_drift": "discussed adapter, shipped adapter",
        "config_changes_observed": [],
        "friction_observed": [],
        "yesterday_recs_followed": [],
        "yesterday_recs_ignored": [],
        "model_usage_observed": ["claude-code: claude-sonnet-4-6"],
        "day_summary": "built the subscription synthesis path",
    }


def _envelope(result_text: str) -> str:
    """Mimic `claude -p --output-format json`: an array of events ending in a result
    event whose `result` field is the model's text response."""
    return json.dumps([
        {"type": "system", "subtype": "init", "session_id": "x"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": result_text}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": result_text},
    ])


def test_parse_clean_json():
    obs = _parse_result(_envelope(json.dumps(_valid_observation())), DayObservation)
    assert isinstance(obs, DayObservation)
    assert obs.topics_raw == ["synthesis", "cli adapter"]
    print("✓ parse: clean JSON result → DayObservation")


def test_parse_json_with_prose_and_fences():
    # Model sometimes wraps JSON in prose / markdown fences despite instructions.
    noisy = "Here's the Reflection:\n```json\n" + json.dumps(_valid_observation()) + "\n```\nDone."
    obs = _parse_result(_envelope(noisy), DayObservation)
    assert obs.day_summary == "built the subscription synthesis path"
    print("✓ parse: JSON embedded in prose/fences is extracted")


def test_parse_missing_result():
    blob = json.dumps([{"type": "result", "subtype": "success"}])  # no `result` field
    try:
        _parse_result(blob, DayObservation)
    except ValueError:
        print("✓ parse: missing result text raises ValueError")
    else:
        raise AssertionError("expected ValueError for missing result text")


def test_parse_schema_violation():
    bad = _valid_observation()
    del bad["day_summary"]  # required field
    try:
        _parse_result(_envelope(json.dumps(bad)), DayObservation)
    except ValueError as e:
        assert "day_summary" in str(e)
        print("✓ parse: schema violation raises")
    else:
        raise AssertionError("expected validation error for missing required field")


def test_extract_no_json():
    try:
        _extract_json_object("sorry, I cannot do that")
    except ValueError:
        print("✓ extract: no JSON object raises ValueError")
    else:
        raise AssertionError("expected ValueError when no JSON present")


def test_model_name_strips_prefix():
    with _env(WISEN_SYNTH_MODEL="claude-cli:claude-sonnet-4-6"):
        assert _model_name() == "claude-sonnet-4-6"
    print("✓ model_name: strips claude-cli: prefix")


def test_default_is_subscription():
    with _no_config(), _env(WISEN_SYNTH_MODEL=None):
        assert claude_cli.synth_model() == "claude-cli:sonnet"
        assert claude_cli.is_cli_model() and _model_name() == "sonnet"
    with _env(WISEN_SYNTH_MODEL="api"):
        assert not claude_cli.is_cli_model()
    print("✓ default: synthesis runs on claude-cli:sonnet; WISEN_SYNTH_MODEL=api opts out")


def _fake_cli(*, installed=True, logged_in=True, p_rc=0, calls=None, auth_stdout=None):
    """Stand-in for subprocess.run: `claude auth status` and `claude -p`."""
    def run(cmd, **kw):
        if calls is not None:
            calls.append((cmd, kw.get("env", {})))
        if not installed:
            raise FileNotFoundError("claude")
        if cmd[1:3] == ["auth", "status"]:
            stdout = auth_stdout if auth_stdout is not None else json.dumps({"loggedIn": logged_in})
            return SimpleNamespace(returncode=0 if logged_in else 1, stdout=stdout, stderr="")
        return SimpleNamespace(returncode=p_rc, stdout=_envelope(json.dumps(_valid_observation())),
                               stderr="Not logged in")
    return run


def _raising(exc: BaseException):
    def run(cmd, **kw):
        raise exc
    return run


def test_cli_problem():
    """One line per state: logged in, logged out, missing, and could-not-check."""
    real = subprocess.run
    calls = []

    def probe(monkey) -> str | None:
        claude_cli._problem_cache = None
        subprocess.run = monkey
        try:
            return claude_cli.cli_problem()
        finally:
            subprocess.run = real

    with _env(ANTHROPIC_API_KEY="sk-ant-test"):
        assert probe(_fake_cli(calls=calls)) is None
        assert "ANTHROPIC_API_KEY" not in calls[0][1]  # the check sees the subscription login
        line = probe(_fake_cli(logged_in=False))
        assert "not logged in" in line and "claude auth login" in line
        assert "not installed" in probe(_fake_cli(installed=False))
        # A probe that could not run or said nothing about the login is not "logged out".
        for failure in (subprocess.TimeoutExpired("claude", 30), PermissionError("claude"), OSError("claude")):
            line = probe(_raising(failure))
            assert "could not check" in line and "not logged in" not in line, (failure, line)
        for garbled in ("", "not json", "null", '"logged in"', "[]", "{}"):
            line = probe(lambda cmd, out=garbled, **kw: SimpleNamespace(returncode=0, stdout=out, stderr=""))
            assert "could not check" in line and "not logged in" not in line, (garbled, line)
    print("✓ cli_problem: ok when logged in; a named fix when missing or logged out; a distinct line otherwise")


def test_cli_problem_is_cached():
    """/health asks on every request: the probe spawns the CLI once per minute."""
    real = subprocess.run
    spawns = []
    claude_cli._problem_cache = None
    subprocess.run = _fake_cli(calls=spawns)
    try:
        assert claude_cli.cli_problem() is None
        assert claude_cli.cli_problem() is None
        assert len(spawns) == 1
    finally:
        subprocess.run = real
        claude_cli._problem_cache = None
    print("✓ cli_problem: repeated calls reuse the brief cache instead of spawning claude")


def test_run_structured_fails_in_one_line():
    """A missing or logged-out CLI raises one ordinary exception with one line,
    which the job engine treats as permanent; a probe that could not check the
    login retries."""
    real = subprocess.run
    try:
        with _env(WISEN_SYNTH_MODEL=None):
            calls = []
            subprocess.run = _fake_cli(logged_in=False, p_rc=1, calls=calls)
            try:
                claude_cli.run_structured("sys", "user", DayObservation)
            except PermanentFailure as e:
                assert "not logged in" in str(e) and "\n" not in str(e)
            else:
                raise AssertionError("expected PermanentFailure for a logged-out CLI")
            assert sum(c[0][1] == "-p" for c in calls) == 1  # not retried

            subprocess.run = _fake_cli(installed=False)
            try:
                claude_cli.run_structured("sys", "user", DayObservation)
            except PermanentFailure as e:
                assert "not installed" in str(e)
            else:
                raise AssertionError("expected PermanentFailure for a missing CLI")

            # Not a permanent state: two attempts, then the CLI's own stderr.
            calls = []
            subprocess.run = _fake_cli(p_rc=1, calls=calls, auth_stdout="garbled")
            try:
                claude_cli.run_structured("sys", "user", DayObservation)
            except RuntimeError as e:
                assert "exited 1" in str(e) and "could not check" not in str(e), str(e)
            else:
                raise AssertionError("expected RuntimeError after both attempts")
            assert sum(c[0][1] == "-p" for c in calls) == 2  # retried once

            subprocess.run = _fake_cli()
            assert claude_cli.run_structured("sys", "user", DayObservation).topics_raw == ["synthesis", "cli adapter"]
    finally:
        subprocess.run = real
    print("✓ run_structured: a missing or logged-out CLI fails at once, in one line; other failures retry")


def test_permanent_cli_failure_fails_the_job_at_once():
    """The job engine fails a permanent CLI misconfiguration on its first attempt
    (no 30s/60s backoff) and the process lives on to report it."""
    from wisen.jobs import JobEngine
    from wisen.store import SqliteStore

    async def synthesize(ctx, args) -> dict:
        return await asyncio.to_thread(claude_cli.run_structured, "sys", "user", DayObservation)

    async def run():
        store = SqliteStore(Path(tempfile.mkdtemp(prefix="wisen-cli-job-")) / "jobs.db", "local")
        engine = JobEngine(store, {"synthesize": synthesize})
        return await engine.run_until_done((await engine.enqueue("synthesize", {"date": "2026-10-19"})).id)

    real = subprocess.run
    subprocess.run = _fake_cli(installed=False)
    try:
        with _env(WISEN_SYNTH_MODEL=None):
            job = asyncio.run(run())
    finally:
        subprocess.run = real
    assert job.state == "failed" and job.attempts == 1, (job.state, job.attempts)
    assert "not installed" in job.error and "\n" not in job.error
    print("✓ jobs: a permanent CLI failure fails the job once, in one line, without retries")


def test_health_reports_claude_cli():
    from wisen import main

    class Store:
        async def ping(self):
            return True

    real_store, real_problem = main.get_store, claude_cli.cli_problem
    main.get_store = Store
    try:
        with _no_config(), _env(WISEN_MODEL="openai:gpt-5.6-terra", OPENAI_API_KEY=None, WISEN_SYNTH_MODEL=None):
            claude_cli.cli_problem = lambda: None
            status = asyncio.run(main.health())
            assert status["synth_model"] == "claude-cli:sonnet" and status["claude_cli"] == "ok"
            assert status["llm_key"] is False  # chat's key is optional for synthesis
            claude_cli.cli_problem = lambda: claude_cli._NOT_LOGGED_IN
            assert asyncio.run(main.health())["claude_cli"] == claude_cli._NOT_LOGGED_IN
            with _env(WISEN_SYNTH_MODEL="api"):
                status = asyncio.run(main.health())
            assert status["synth_model"] == "openai:gpt-5.6-terra" and "claude_cli" not in status
    finally:
        main.get_store, claude_cli.cli_problem = real_store, real_problem
    print("✓ health: a claude-cli synthesis reports the CLI's state, not an API key")


def test_chat_skips_without_a_key():
    """No API key for the chat/Gardener model: a chat turn answers with the one
    line saying so, instead of failing on a missing key."""
    from wisen.chat import agent as chat

    async def stream() -> list[dict]:
        return [e async for e in chat.stream_chat([{"role": "user", "content": "what did I ship?"}])]

    with _env(WISEN_MODEL="openai:gpt-5.6-terra", OPENAI_API_KEY=None):
        line = model_problem()
        assert "OPENAI_API_KEY" in line and "\n" not in line, line
        assert asyncio.run(stream()) == [{"type": "token", "data": line}]
        assert asyncio.run(chat.answer("what did I ship?", raw_search=False)) == line
    with _env(OPENAI_API_KEY="sk-openai-test"):
        assert model_problem() is None
    print("✓ chat: with no API key a turn is answered with the one line naming the key")


def test_google_key_is_accepted():
    """Either Google key the provider takes counts as set: with GOOGLE_API_KEY
    alone, chat, the Gardener and /health must not report google as keyless."""
    from wisen import main

    class Store:
        async def ping(self):
            return True

    real_store = main.get_store
    main.get_store = Store
    try:
        with _no_config(), _env(WISEN_MODEL="google:gemini-2.5-flash", GEMINI_API_KEY=None,
                                GOOGLE_API_KEY="gk-test", WISEN_SYNTH_MODEL="api"):
            assert model_problem() is None
            assert asyncio.run(main.health())["llm_key"] is True
        with _env(WISEN_MODEL="google:gemini-2.5-flash", GEMINI_API_KEY=None, GOOGLE_API_KEY=None):
            line = model_problem()
            assert "GEMINI_API_KEY" in line and "GOOGLE_API_KEY" in line, line
    finally:
        main.get_store = real_store
    print("✓ google: GOOGLE_API_KEY alone is a key; with neither set, the line names both")


def test_gardener_skips_without_a_key():
    """The Gardener needs the same key: its job for the day succeeds with one
    line saying why, so `wisen synthesize` still ends with the reflection."""
    from wisen.gardener import loop
    from wisen.jobs import JobEngine
    from wisen.store import SqliteStore

    day, before = "2026-10-19", "2026-10-18"
    rec = {"target": "claude-md", "action": "note the model switch", "reason": "it repeats"}

    async def garden():
        store = SqliteStore(Path(tempfile.mkdtemp(prefix="wisen-nokey-")) / "garden.db", "local")
        for d in (before, day):
            await store.upsert_reflection({"date": d, "recommendations": [rec], "friction_points": []})
        engine = JobEngine(store, loop.HANDLERS)
        return await engine.run_until_done((await engine.enqueue("garden", {"date": day})).id)

    with _env(WISEN_MODEL="openai:gpt-5.6-terra", OPENAI_API_KEY=None,
              WISEN_ROOT_DIR=tempfile.mkdtemp(prefix="wisen-root-")):
        job = asyncio.run(garden())
        assert job.state == "succeeded", job.error
        assert job.result == {"date": day, "proposals": 0, "skipped": model_problem(), "needs_key": True}
        assert "OPENAI_API_KEY" in job.result["skipped"]
    print("✓ gardener: no API key skips the run in one line naming the key; the job succeeds")


def test_gardener_runs_again_once_the_key_is_set():
    """A day skipped only for the missing key is not closed: with the key set,
    the same day's run proposes."""
    from wisen import jobs as jobs_mod
    from wisen.gardener import loop
    from wisen.gardener.agent import Draft
    from wisen.jobs import JobEngine
    from wisen.proposals import Proposals
    from wisen.store import SqliteStore
    from wisen.synthesis import job as synthesis_job

    day, before = "2026-10-19", "2026-10-18"
    rec = {"target": "claude-md", "action": "note the model switch", "reason": "it repeats"}
    draft = Draft(tool_call_id="call-1", target="wisen:CLAUDE.md", diff="+note it\n", base_blob=None,
                  rationale="it repeats", evidence=["2026-10-19"], current="a\n", proposed="a\nnote it\n")
    proposals = []

    async def fake_propose(store, day, snapshot=None):
        proposals.append(day)
        return loop.GardenerRun(run_id="run-1", conversation="[]", output=None, drafts=[draft])

    async def no_replay(d):
        return None

    real_propose, real_replay, real_engine = loop.propose, loop.replay_draft, jobs_mod._engine
    loop.propose, loop.replay_draft = fake_propose, no_replay
    try:
        async def garden():
            store = SqliteStore(Path(tempfile.mkdtemp(prefix="wisen-rekey-")) / "garden.db", "local")
            for d in (before, day):
                await store.upsert_reflection({"date": d, "recommendations": [rec], "friction_points": []})
            jobs_mod._engine = JobEngine(store, loop.HANDLERS)
            with _env(WISEN_MODEL="openai:gpt-5.6-terra", OPENAI_API_KEY=None):
                skipped = await synthesis_job.garden_day(day)
            proposed_without_a_key = list(proposals)
            with _env(OPENAI_API_KEY="sk-openai-test"):
                ran = await synthesis_job.garden_day(day)
            return skipped, proposed_without_a_key, ran, [p.target for p in await Proposals(store).list(states=None)]

        with _env(WISEN_ROOT_DIR=tempfile.mkdtemp(prefix="wisen-root-")):
            skipped, proposed_without_a_key, ran, targets = asyncio.run(garden())
        assert skipped.state == "succeeded" and skipped.result["needs_key"], skipped.result
        assert proposed_without_a_key == []
        assert ran.id == skipped.id and ran.state == "succeeded", ran.error
        assert proposals == [day] and ran.result["proposals"] == 1
        assert targets == ["wisen:CLAUDE.md"]
    finally:
        loop.propose, loop.replay_draft, jobs_mod._engine = real_propose, real_replay, real_engine
    print("✓ gardener: a no-key skip does not close the day; with the key set the same day proposes")


if __name__ == "__main__":
    test_parse_clean_json()
    test_parse_json_with_prose_and_fences()
    test_parse_missing_result()
    test_parse_schema_violation()
    test_extract_no_json()
    test_model_name_strips_prefix()
    test_default_is_subscription()
    test_cli_problem()
    test_cli_problem_is_cached()
    test_run_structured_fails_in_one_line()
    test_permanent_cli_failure_fails_the_job_at_once()
    test_health_reports_claude_cli()
    test_chat_skips_without_a_key()
    test_google_key_is_accepted()
    test_gardener_skips_without_a_key()
    test_gardener_runs_again_once_the_key_is_set()
    print("\nall claude_cli tests passed")
