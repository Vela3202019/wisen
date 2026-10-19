"""
Export reflections from the local store to a folder of JSON files (one per date), for
syncing to a remote dashboard. Each document becomes a ResultPacket (only
allow-listed result fields) and goes out through the egress gate; full
documents stay local.
"""
from pathlib import Path

from wisen import egress
from wisen.packets import result_packet
from wisen.store import Store


async def export_reflections(store: Store, out_dir: Path) -> int:
    count = 0
    for doc in await store.list_reflections():
        date = doc.get("date")
        if not date:
            continue
        egress.write_file(result_packet(doc), out_dir / f"{date}.json")
        count += 1
    return count
