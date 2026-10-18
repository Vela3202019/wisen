"""
wisen command-line interface.

  wisen synthesize [YYYY-MM-DD]   ingest + synthesize one day (default: yesterday)
  wisen serve [--host] [--port]   run the API server (needs the [server] extra)
  wisen export [dir]              dump reflections from Mongo to JSON files
  wisen eval <args...>            case-bank commands (mine, list, review,
                                  metrics, replay, compare)
"""
import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wisen import __version__, config


async def _check_mongo() -> None:
    """Fail fast with a friendly message if MongoDB is unreachable."""
    from wisen.db import get_client

    uri = config.get("storage", "mongodb_uri", "MONGODB_URI", "mongodb://127.0.0.1:27017")
    try:
        await asyncio.wait_for(get_client().admin.command("ping"), timeout=5)
    except (TimeoutError, OSError):
        raise SystemExit(
            f"MongoDB unreachable at {uri} — is it running?\n"
            "  brew services start mongodb-community    # macOS\n"
            "  docker run -d -p 27017:27017 mongo       # anywhere with Docker"
        )


async def _synthesize(date: str) -> None:
    from wisen.ingestion.pipeline import ingest_all
    from wisen.synthesis.agent import synthesize

    await _check_mongo()
    print(f"[{datetime.now(UTC).isoformat()}] synthesizing {date}")
    result = await ingest_all(date)
    reflection = await synthesize(result, date)
    print(f"done — reflection saved for {reflection.date}")


async def _export(out_dir: Path) -> None:
    from wisen.db import get_db
    from wisen.export import export_reflections

    await _check_mongo()
    count = await export_reflections(get_db(), out_dir)
    print(f"exported {count} reflections to {out_dir}")


def _serve(host: str, port: int, reload: bool) -> None:
    try:
        import uvicorn
    except ImportError:
        raise SystemExit("wisen serve needs the server extra: pip install 'wisen[server]'")
    uvicorn.run("wisen.main:app", host=host, port=port, reload=reload)


def main() -> None:
    p = argparse.ArgumentParser(prog="wisen", description=__doc__.strip().splitlines()[0])
    p.add_argument("--version", action="version", version=f"wisen {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sy = sub.add_parser("synthesize", help="ingest + synthesize one day")
    sy.add_argument("date", nargs="?", help="YYYY-MM-DD (default: yesterday)")

    sv = sub.add_parser("serve", help="run the API server")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--reload", action="store_true")

    ex = sub.add_parser("export", help="dump reflections to JSON files")
    ex.add_argument("dir", nargs="?", help="output dir (default: paths.export_dir)")

    ev = sub.add_parser("eval", help="case-bank commands (mine, list, review, …)")
    ev.add_argument("args", nargs=argparse.REMAINDER)

    args = p.parse_args()

    if args.cmd == "synthesize":
        date = args.date or (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
        asyncio.run(_synthesize(date))
    elif args.cmd == "serve":
        _serve(args.host, args.port, args.reload)
    elif args.cmd == "export":
        default_dir = config.get("paths", "export_dir", "WISEN_EXPORT_DIR", "~/.wisen/exports/reflections")
        asyncio.run(_export(Path(args.dir or default_dir).expanduser()))
    elif args.cmd == "eval":
        from wisen.evals.cli import main as eval_main
        sys.argv = ["wisen eval"] + args.args
        eval_main()
