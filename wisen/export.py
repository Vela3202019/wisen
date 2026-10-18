"""
Export reflections from MongoDB to a folder of JSON files (one per date), for
syncing to a remote dashboard. Every document goes through CloudReflection, so
only allow-listed result fields leave the machine; full documents stay in Mongo.
"""
from pathlib import Path

from wisen.synthesis.schema import CloudReflection


def cloud_json(doc: dict) -> str:
    return CloudReflection.model_validate(doc).model_dump_json(indent=2)


async def export_reflections(db, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    async for doc in db.reflections.find({}):
        date = doc.get("date")
        if not date:
            continue
        (out_dir / f"{date}.json").write_text(cloud_json(doc))
        count += 1
    return count
