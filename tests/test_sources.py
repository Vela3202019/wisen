"""
Test each connector individually and report what it finds.
Run from the repo root:  python3 -m tests.test_sources
"""
import asyncio
from datetime import date, timedelta
from connectors import claude_code, opencode, cursor, codex, gemini_cli, pi, generic_export, git_commits, agent_configs

DATES = [(date.today() - timedelta(days=i)).isoformat() for i in range(7)]


async def probe(name, gen_fn):
    sessions = []
    try:
        async for s in gen_fn:
            sessions.append(s)
    except Exception as e:
        print(f"  [{name}] ERROR: {e}")
        return

    if not sessions:
        print(f"  [{name}] no sessions found")
        return

    by_date = {}
    for s in sessions:
        by_date.setdefault(s.date, []).append(s)

    print(f"  [{name}] {len(sessions)} sessions across {len(by_date)} days")
    for d in sorted(by_date, reverse=True)[:5]:
        day_sessions = by_date[d]
        total_turns = sum(s.metadata.total_turns for s in day_sessions)
        projects = list({s.project_name for s in day_sessions})[:3]
        print(f"    {d}  {len(day_sessions)} sessions  {total_turns} turns  {projects}")


async def main():
    print("=== Testing all connectors (past 7 days) ===\n")

    # Run each connector with no date filter so we see everything
    await probe("claude-code",   claude_code.ingest())
    await probe("codex",         codex.ingest())
    await probe("opencode",      opencode.ingest())
    await probe("cursor",        cursor.ingest())
    await probe("gemini-cli",    gemini_cli.ingest())
    await probe("pi",            pi.ingest())
    await probe("generic-export",generic_export.ingest())
    await probe("git-commits",   git_commits.ingest())
    print("\n  (git-commits needs GITHUB_TOKEN + GITHUB_USERNAME in .env)")

    print()

    # Agent configs
    configs = []
    try:
        for d in DATES:
            async for c in agent_configs.ingest(d):
                configs.append(c)
    except Exception as e:
        print(f"  [agent-configs] ERROR: {e}")
    else:
        sources = list({c.source for c in configs})
        print(f"  [agent-configs] {len(configs)} configs found  sources={sources}")


asyncio.run(main())
