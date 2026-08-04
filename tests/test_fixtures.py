"""
Ingestion smoke test on committed fixture data — runs anywhere: no real session
history, no MongoDB, no API keys. This is what CI runs. From the repo root:
  python3 -m tests.test_fixtures
"""
import os
from pathlib import Path

# Point wisen at the fixture tree BEFORE importing it — connectors resolve
# their directories from WISEN_ROOT_DIR at import time.
_FIXTURES = Path(__file__).resolve().parent / "fixtures"
os.environ["WISEN_ROOT_DIR"] = str(_FIXTURES)
os.environ["GIT_SCAN_DIRS"] = str(_FIXTURES / "repos")  # empty on purpose

import asyncio

from wisen.ingestion.pipeline import ingest_all

FIXTURE_DATE = "2026-01-15"


async def main():
    result = await ingest_all(FIXTURE_DATE)
    by_source = result.summary()["by_source"]
    print(f"sessions={len(result.sessions)}  by_source={by_source}  errors={result.errors}")

    assert not result.errors, f"connector errors: {result.errors}"
    assert by_source.get("claude-code") == 1, f"expected 1 claude-code session, got {by_source}"
    assert by_source.get("codex") == 1, f"expected 1 codex session, got {by_source}"

    cc = next(s for s in result.sessions if s.source == "claude-code")
    assert cc.project_name == "app", cc.project_name  # '-Users-dev-demo-app' → /Users/dev/demo/app
    assert cc.date == FIXTURE_DATE, cc.date
    assert any("pagination" in t.content for t in cc.turns)
    assert any(t.tool_names for t in cc.turns), "expected tool calls in claude-code turns"

    cx = next(s for s in result.sessions if s.source == "codex")
    assert cx.date == FIXTURE_DATE, cx.date
    user_turns = [t for t in cx.turns if t.role == "user"]
    assert len(user_turns) == 2, f"developer/system turns leaked or user turns dropped: {cx.turns}"

    print("OK")


asyncio.run(main())
