"""
The first-run commands on committed fixture data, offline: `wisen demo`,
`wisen doctor`, `wisen serve` and the default day of `wisen synthesize`. From the repo root:
  python3 -m tests.test_first_run
"""
import os
import tempfile
from pathlib import Path

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_TMP = tempfile.mkdtemp(prefix="wisen-first-run-")
os.environ["WISEN_ROOT_DIR"] = str(_FIXTURES)
os.environ["GIT_SCAN_DIRS"] = str(_FIXTURES / "repos")
os.environ["WISEN_DB_PATH"] = str(Path(_TMP) / "wisen.db")
os.environ["PATH"] = _TMP  # no `claude` CLI: the doctor reports the subscription as not installed
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("WISEN_MODEL", None)
os.environ.pop("WISEN_SYNTH_MODEL", None)

import asyncio
import sys
from datetime import UTC, datetime
from unittest.mock import Mock, patch

from wisen import cli, demo, doctor
from wisen.ingestion.pipeline import session_days


def at(day: str):
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(f"{day}T12:00:00+00:00").astimezone(tz or UTC)
    return Fixed


async def main():
    text = demo.render()
    assert "Don't add summary comments above functions." in text, text
    assert "Example commands only; this demo does not create a proposal." in text, text
    assert "wisen gardener approve" in text and "wisen doctor" in text, text
    print("✓ first run: wisen demo renders a valid sample reflection and proposal")

    days, errors = await session_days()
    assert not errors, errors
    assert days == {"2026-01-15": {"claude-code": 1, "codex": 1, "pi": 1}}, days

    report, ready = await doctor.report()
    assert not ready, report
    assert "claude-code      1 session" in report and "cursor           none found" in report, report
    assert "not installed" in report, report
    assert "then: wisen synthesize 2026-01-15" in report, report
    print("✓ first run: wisen doctor counts sessions per source and names the missing CLI")

    model = "groq:llama-3.3-70b-versatile"
    with patch.dict(os.environ, WISEN_SYNTH_MODEL="api", WISEN_MODEL=model), patch.dict(sys.modules, groq=None):
        report, ready = await doctor.report()
    assert not ready, report
    assert f"✗ reflection       {model}" in report and f"✗ chat, Gardener   {model}" in report, report
    assert 'pip install "pydantic-ai-slim[groq]"' in report, report
    with patch.dict(os.environ, WISEN_SYNTH_MODEL="api", WISEN_MODEL="openai:gpt-4o", OPENAI_API_KEY="test"):
        report, ready = await doctor.report()
    assert ready, report
    assert "✓ reflection       openai:gpt-4o" in report and "✓ chat, Gardener   openai:gpt-4o" in report, report
    print("✓ first run: wisen doctor checks API models and reports missing provider packages")

    uvicorn = Mock()
    with patch.dict(sys.modules, fastapi=None, uvicorn=uvicorn), patch.object(sys, "argv", ["wisen", "serve"]):
        try:
            cli.main()
        except SystemExit as error:
            assert error.code == "wisen serve needs the server extra: pip install 'wisen[server]'", error.code
        else:
            raise AssertionError("wisen serve did not exit when fastapi was missing")
    uvicorn.run.assert_not_called()
    with patch.dict(sys.modules, fastapi=Mock(), uvicorn=uvicorn):
        cli._serve("127.0.0.1", 8000, reload=False)
    uvicorn.run.assert_called_once_with("wisen.main:app", host="127.0.0.1", port=8000, reload=False)
    print("✓ first run: wisen serve gives the install hint when uvicorn is present but fastapi is missing")

    real = cli.datetime
    try:
        cli.datetime = at("2026-01-16")
        assert await cli._default_day() == "2026-01-15"
        cli.datetime = at("2026-01-20")
        assert await cli._default_day() == "2026-01-15"
        cli.datetime = at("2026-02-15")
        assert await cli._default_day() is None
    finally:
        cli.datetime = real
    print("✓ first run: synthesize defaults to the latest day with sessions in the past 14 days")


if __name__ == "__main__":
    asyncio.run(main())
