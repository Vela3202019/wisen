"""
Test the claude -p structured-output adapter's parsing/validation (no live CLI call).
Run: python3 -m tests.test_claude_cli
"""
import asyncio
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from wisen import config
from wisen.synthesis import claude_cli
from wisen.synthesis.claude_cli import _extract_json_object, _model_name, _parse_result
from wisen.synthesis.schema import DayObservation

# No ~/.wisen/config.toml: the defaults below are the built-in ones.
config.CONFIG_PATH = Path("/nonexistent/wisen-config.toml")
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
    import os
    os.environ["WISEN_SYNTH_MODEL"] = "claude-cli:claude-sonnet-4-6"
    assert _model_name() == "claude-sonnet-4-6"
    print("✓ model_name: strips claude-cli: prefix")


def test_default_is_subscription():
    os.environ.pop("WISEN_SYNTH_MODEL", None)
    assert claude_cli.synth_model() == "claude-cli:sonnet"
    assert claude_cli.is_cli_model() and _model_name() == "sonnet"
    os.environ["WISEN_SYNTH_MODEL"] = "api"
    assert not claude_cli.is_cli_model()
    os.environ.pop("WISEN_SYNTH_MODEL")
    print("✓ default: synthesis runs on claude-cli:sonnet; WISEN_SYNTH_MODEL=api opts out")


def _fake_cli(*, installed=True, logged_in=True, p_rc=0, calls=None):
    """Stand-in for subprocess.run: `claude auth status` and `claude -p`."""
    def run(cmd, **kw):
        if calls is not None:
            calls.append((cmd, kw.get("env", {})))
        if not installed:
            raise FileNotFoundError("claude")
        if cmd[1:3] == ["auth", "status"]:
            return SimpleNamespace(returncode=0 if logged_in else 1, stdout=json.dumps({"loggedIn": logged_in}),
                                   stderr="")
        return SimpleNamespace(returncode=p_rc, stdout=_envelope(json.dumps(_valid_observation())),
                               stderr="Not logged in")
    return run


def test_cli_problem():
    real = subprocess.run
    calls = []
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
    try:
        subprocess.run = _fake_cli(calls=calls)
        assert claude_cli.cli_problem() is None
        assert "ANTHROPIC_API_KEY" not in calls[0][1]  # the check sees the subscription login
        subprocess.run = _fake_cli(logged_in=False)
        assert "not logged in" in claude_cli.cli_problem() and "claude auth login" in claude_cli.cli_problem()
        subprocess.run = _fake_cli(installed=False)
        assert "not installed" in claude_cli.cli_problem()
    finally:
        subprocess.run = real
        os.environ.pop("ANTHROPIC_API_KEY")
    print("✓ cli_problem: ok when logged in; one line naming the fix when missing or logged out")


def test_run_structured_fails_in_one_line():
    real = subprocess.run
    os.environ.pop("WISEN_SYNTH_MODEL", None)
    try:
        calls = []
        subprocess.run = _fake_cli(logged_in=False, p_rc=1, calls=calls)
        try:
            claude_cli.run_structured("sys", "user", DayObservation)
        except RuntimeError as e:
            assert "not logged in" in str(e) and "\n" not in str(e)
        else:
            raise AssertionError("expected RuntimeError for a logged-out CLI")
        assert sum(c[0][1] == "-p" for c in calls) == 1  # not retried
        subprocess.run = _fake_cli(installed=False)
        try:
            claude_cli.run_structured("sys", "user", DayObservation)
        except RuntimeError as e:
            assert "not installed" in str(e)
        else:
            raise AssertionError("expected RuntimeError for a missing CLI")
        subprocess.run = _fake_cli()
        assert claude_cli.run_structured("sys", "user", DayObservation).topics_raw == ["synthesis", "cli adapter"]
    finally:
        subprocess.run = real
    print("✓ run_structured: a missing or logged-out CLI fails at once, in one line")


def test_health_reports_claude_cli():
    from wisen import main

    class Store:
        async def ping(self):
            return True

    real_store, real_problem = main.get_store, claude_cli.cli_problem
    main.get_store = Store
    os.environ["WISEN_MODEL"] = "openai:gpt-5.6-terra"
    os.environ.pop("OPENAI_API_KEY", None)
    os.environ.pop("WISEN_SYNTH_MODEL", None)
    try:
        claude_cli.cli_problem = lambda: None
        status = asyncio.run(main.health())
        assert status["synth_model"] == "claude-cli:sonnet" and status["claude_cli"] == "ok"
        assert status["llm_key"] is False  # chat's key is optional for synthesis
        claude_cli.cli_problem = lambda: claude_cli._NOT_LOGGED_IN
        assert asyncio.run(main.health())["claude_cli"] == claude_cli._NOT_LOGGED_IN
        os.environ["WISEN_SYNTH_MODEL"] = "api"
        status = asyncio.run(main.health())
        assert status["synth_model"] == "openai:gpt-5.6-terra" and "claude_cli" not in status
    finally:
        main.get_store, claude_cli.cli_problem = real_store, real_problem
        os.environ.pop("WISEN_SYNTH_MODEL", None)
        os.environ.pop("WISEN_MODEL")
    print("✓ health: a claude-cli synthesis reports the CLI's state, not an API key")


if __name__ == "__main__":
    test_parse_clean_json()
    test_parse_json_with_prose_and_fences()
    test_parse_missing_result()
    test_parse_schema_violation()
    test_extract_no_json()
    test_model_name_strips_prefix()
    test_default_is_subscription()
    test_cli_problem()
    test_run_structured_fails_in_one_line()
    test_health_reports_claude_cli()
    print("\nall claude_cli tests passed")
