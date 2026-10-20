"""
wisen command-line interface.

  wisen synthesize [YYYY-MM-DD]   ingest + synthesize one day (default: yesterday)
  wisen serve [--host] [--port]   run the API server (needs the [server] extra)
  wisen export [dir]              dump reflections from the local store to JSON files
  wisen egress [--date] [--list]  show what passed the egress gate today
  wisen migrate-mongo             one-off copy of a pre-SQLite MongoDB into SQLite
  wisen eval <args...>            case-bank commands (mine, list, review,
                                  metrics, replay, compare)
"""
import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wisen import __version__, config


async def _synthesize(date: str) -> None:
    from wisen.synthesis.job import synthesize_day

    print(f"[{datetime.now(UTC).isoformat()}] synthesizing {date}")
    job = await synthesize_day(date)
    if job.state != "succeeded":
        raise SystemExit(f"synthesis failed: {job.error}")
    print(f"done — reflection saved for {date}")


async def _export(out_dir: Path) -> None:
    from wisen.export import export_reflections
    from wisen.store import get_store

    count = await export_reflections(get_store(), out_dir)
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

    eg = sub.add_parser("egress", help="show what passed the egress gate (today by default)")
    eg.add_argument("--date", help="YYYY-MM-DD, local time (default: today)")
    eg.add_argument("--list", action="store_true", help="one line per packet")

    sub.add_parser("migrate-mongo", help="copy a pre-SQLite MongoDB into SQLite (needs [migrate])")

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
    elif args.cmd == "egress":
        from wisen import egress
        day = datetime.fromisoformat(args.date).date() if args.date else datetime.now().astimezone().date()
        print(egress.report(day, every=args.list))
    elif args.cmd == "migrate-mongo":
        from wisen.migrate_mongo import main as migrate_main
        migrate_main()
    elif args.cmd == "eval":
        from wisen.evals.cli import main as eval_main
        sys.argv = ["wisen eval"] + args.args
        eval_main()
