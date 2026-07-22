"""
Test ingestion pipeline. Run from the repo root:
  python3 -m tests.test_ingest             # past 7 days
  python3 -m tests.test_ingest 2026-06-26  # specific date
"""
import asyncio
import sys
from datetime import date, timedelta
from wisen.ingestion.pipeline import ingest_all


async def main():
    if len(sys.argv) > 1:
        dates = [sys.argv[1]]
    else:
        today = date.today()
        dates = [(today - timedelta(days=i)).isoformat() for i in range(7)]

    total_sessions = 0
    total_configs = 0
    all_errors = []

    for d in dates:
        result = await ingest_all(d)
        total_sessions += len(result.sessions)
        total_configs += len(result.configs)
        all_errors.extend(result.errors)
        summary = result.summary()
        sources = summary["by_source"]
        print(f"{d}  sessions={len(result.sessions)}  configs={len(result.configs)}  {sources}")

    print(f"\nTotal  sessions={total_sessions}  configs={total_configs}")
    if all_errors:
        print(f"\nErrors ({len(all_errors)}):")
        for e in all_errors:
            print(f"  - {e}")
    else:
        print("No errors.")
    print("\n(Nothing written to MongoDB — data stays local until synthesis.)")


asyncio.run(main())
