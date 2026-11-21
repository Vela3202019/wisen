"""
The first-run commands on committed fixture data, offline: `wisen demo`,
`wisen doctor` and the default day of `wisen synthesize`. From the repo root:
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
from datetime import UTC, datetime

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
