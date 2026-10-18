"""
Export all reflections from the local store to a folder of JSON files (one per date),
for syncing to a remote dashboard. Only CloudReflection fields are written.

Usage: python3 scripts/export_reflections.py [output_dir]   (default: ~/.wisen/exports/reflections)
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wisen import config
from wisen.export import export_reflections
from wisen.store import get_store


async def main() -> None:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else config.get("paths", "export_dir", "WISEN_EXPORT_DIR", "~/.wisen/exports/reflections")).expanduser()
    count = await export_reflections(get_store(), out_dir)
    print(f"exported {count} reflections to {out_dir}")


if __name__ == "__main__":
    asyncio.run(main())
