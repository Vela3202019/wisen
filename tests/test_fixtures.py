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

    assert by_source.get("pi") == 1, f"expected 1 pi session, got {by_source}"
    pi = next(s for s in result.sessions if s.source == "pi")
    assert pi.session_id == "pi:/Users/dev/demo-cli:fixture-pi-1", pi.session_id
    assert pi.project_name == "demo-cli", pi.project_name
    assert pi.date == FIXTURE_DATE, pi.date
    contents = [t.content for t in pi.turns]
    # Active branch only: the sibling reply abandoned via /tree is gone
    assert not any("Abandoned approach" in c for c in contents), contents
    assert any("state machine" in c for c in contents), contents
    # Compaction: entries before firstKeptEntryId count once, as the summary
    assert not any("write a csv parser" in c for c in contents), contents
    assert sum("[compaction summary]" in c for c in contents) == 1, contents
    assert contents[0] == "[tool_result] export function runImport() {}", contents
    assert not any("expert coding assistant" in c for c in contents), "system prompt leaked"
    assert [t.role for t in pi.turns] == [
        "user", "assistant", "assistant", "user", "assistant", "user", "assistant",
    ], [(t.role, t.content) for t in pi.turns]
    assert pi.turns[-1].tool_names == ["bash"], pi.turns[-1]

    print("OK")


asyncio.run(main())
