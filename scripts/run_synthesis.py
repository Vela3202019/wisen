"""
Run one day's ingest + synthesis outside the API server.
Usage: python3 scripts/run_synthesis.py [YYYY-MM-DD]   (default: yesterday)
Meant to be run daily by launchd (com.vela.wisen-daily). Runs as a durable
job keyed by the date: a launch after a crash resumes it from its last
checkpoint, and a launch after a failure retries it. A day with no sessions,
or one whose reflection already covers its sessions, is skipped and says so
(`wisen synthesize <date> --force` runs it anyway).
"""
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wisen.synthesis.job import Skipped, garden_day, synthesize_day


async def main() -> None:
    if len(sys.argv) > 1:
        date = sys.argv[1]
    else:
        date = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    print(f"[{datetime.now(UTC).isoformat()}] synthesizing {date}")
    job = await synthesize_day(date)
    if isinstance(job, Skipped):
        print(f"skipped {date}: {job.reason}")
        return
    if job.state != "succeeded":
        raise SystemExit(f"synthesis failed: {job.error}")
    print(f"done — reflection saved for {date}")
    # The day's Gardener batch, so it is waiting with the morning reflection.
    garden = await garden_day(date)
    if garden.state != "succeeded":
        print(f"gardener failed: {garden.error}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
