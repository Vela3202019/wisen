"""
Run one day's ingest + synthesis outside the API server.
Usage: python3 scripts/run_synthesis.py [YYYY-MM-DD]   (default: yesterday)
Meant to be run daily by launchd (com.vela.wisen-daily).
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.pipeline import ingest_all
from synthesis.agent import synthesize


async def main() -> None:
    if len(sys.argv) > 1:
        date = sys.argv[1]
    else:
        date = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    print(f"[{datetime.now(timezone.utc).isoformat()}] synthesizing {date}")
    result = await ingest_all(date)
    reflection = await synthesize(result, date)
    print(f"done — reflection saved for {reflection.date}")


if __name__ == "__main__":
    asyncio.run(main())
