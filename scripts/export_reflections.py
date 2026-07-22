"""
Export all reflections from MongoDB to a folder of JSON files (one per date),
for syncing to a remote dashboard.

Usage: python3 scripts/export_reflections.py [output_dir]   (default: ~/.wisen/exports/reflections)
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import get_db


async def main() -> None:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "~/.wisen/exports/reflections").expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    db = get_db()
    count = 0
    async for doc in db.reflections.find({}):
        doc.pop("_id", None)
        date = doc.get("date")
        if not date:
            continue
        (out_dir / f"{date}.json").write_text(json.dumps(doc, indent=2, default=str))
        count += 1
    print(f"exported {count} reflections to {out_dir}")


if __name__ == "__main__":
    asyncio.run(main())
