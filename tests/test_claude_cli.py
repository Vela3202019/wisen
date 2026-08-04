"""
Test the claude -p structured-output adapter's parsing/validation (no live CLI call).
Run: python3 -m tests.test_claude_cli
"""
import json

from wisen.synthesis.claude_cli import _extract_json_object, _model_name, _parse_result
from wisen.synthesis.schema import DayObservation


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


if __name__ == "__main__":
    test_parse_clean_json()
    test_parse_json_with_prose_and_fences()
    test_parse_missing_result()
    test_parse_schema_violation()
    test_extract_no_json()
    test_model_name_strips_prefix()
    print("\nall claude_cli tests passed")
