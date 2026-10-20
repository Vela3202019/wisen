"""
Run one day's ingest + synthesis outside the API server.
Usage: python3 scripts/run_synthesis.py [YYYY-MM-DD]   (default: yesterday)
Meant to be run daily by launchd (com.vela.wisen-daily). Runs as a durable
job keyed by the date: a launch after a crash resumes it from its last
checkpoint, a launch after a failure retries it, and a second launch for a
day that succeeded synthesizes the day again.
"""
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wisen.synthesis.job import synthesize_day


async def main() -> None:
    if len(sys.argv) > 1:
        date = sys.argv[1]
    else:
        date = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    print(f"[{datetime.now(UTC).isoformat()}] synthesizing {date}")
    job = await synthesize_day(date)
    if job.state != "succeeded":
        raise SystemExit(f"synthesis failed: {job.error}")
    print(f"done — reflection saved for {date}")


if __name__ == "__main__":
    asyncio.run(main())
