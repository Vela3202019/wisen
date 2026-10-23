"""
wisen command-line interface.

  wisen synthesize [YYYY-MM-DD]   ingest + synthesize one day (default: yesterday)
  wisen serve [--host] [--port]   run the API server (needs the [server] extra)
  wisen export [dir]              dump reflections from the local store to JSON files
  wisen egress [--date] [--list]  show what passed the egress gate today
  wisen migrate-mongo             one-off copy of a pre-SQLite MongoDB into SQLite
  wisen eval <args...>            case-bank commands (mine, list, review,
                                  metrics, replay, compare)
  wisen worker                    laptop daemon: with a paired server, sync and run its
                                  device jobs (outbound only); unpaired, local jobs only
  wisen gardener <command>        Gardener proposals: run, list, approve, reject, status
  wisen cloud <command>           the server (serve, user, vapid-keys, backup,
                                  restore) and pairing this laptop with one (pair)
"""
import argparse
import asyncio
import getpass
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wisen import __version__, config


async def _synthesize(date: str) -> None:
    from wisen.synthesis.job import garden_day, synthesize_day

    print(f"[{datetime.now(UTC).isoformat()}] synthesizing {date}")
    job = await synthesize_day(date)
    if job.state != "succeeded":
        raise SystemExit(f"synthesis failed: {job.error}")
    print(f"done — reflection saved for {date}")
    garden = await garden_day(date)
    if garden.state != "succeeded":
        print(f"gardener failed: {garden.error}", file=sys.stderr)
    elif garden.result["proposals"]:
        print(f"gardener: {garden.result['proposals']} proposals waiting (wisen gardener list)")


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


def _cloud_db(arg: str | None) -> Path:
    return Path(arg or os.environ.get("WISEN_CLOUD_DB", "~/.wisen/cloud/wisen.db")).expanduser()


def _cloud(args) -> None:
    if args.cloud_cmd == "serve":
        try:
            import uvicorn
        except ImportError:
            raise SystemExit("wisen cloud serve needs the cloud extra: pip install 'wisen[cloud]'")
        from wisen.cloud.server import from_env

        # One worker: this process owns the SQLite file and the timers.
        uvicorn.run(from_env(_cloud_db(args.db)), host=args.host, port=args.port, workers=1)
    elif args.cloud_cmd == "user":
        from wisen.cloud.mailbox import Mailbox

        password = getpass.getpass(f"password for {args.name}: ")
        if getpass.getpass("again: ") != password:
            raise SystemExit("passwords differ")
        try:
            created = Mailbox(_cloud_db(args.db), sender=None).set_user(args.name, password, args.timezone, args.daily_at)
        except ValueError as e:
            raise SystemExit(str(e))
        print(f"{'created' if created else 'updated'} user {args.name}")
    elif args.cloud_cmd == "vapid-keys":
        from wisen.cloud.push import generate_vapid_keys

        private, public = generate_vapid_keys()
        print(f"WISEN_VAPID_PRIVATE_KEY={private}\nWISEN_VAPID_PUBLIC_KEY={public}")
    elif args.cloud_cmd == "backup":
        from wisen.cloud.backup import backup

        print(backup(_cloud_db(args.db), Path(args.dir).expanduser(), args.keep))
    elif args.cloud_cmd == "restore":
        from wisen.cloud.backup import restore

        try:
            restore(Path(args.snapshot).expanduser(), _cloud_db(args.db))
        except RuntimeError as e:
            raise SystemExit(str(e))
        print(f"restored {args.snapshot} to {_cloud_db(args.db)}")
    elif args.cloud_cmd == "pair":
        from wisen.cloud.client import Refused, Unavailable, pair

        code = args.code or input("pairing code (dashboard → Devices → Add device): ")
        try:
            creds = asyncio.run(pair(args.url, code, args.name))
        except (ValueError, Refused, Unavailable) as e:
            raise SystemExit(f"pairing failed: {e}")
        print(f"paired as device {creds.device_id} of {creds.user_id}; credentials in {creds.save()}")
        print("start the worker: wisen worker (or load deploy/launchd/com.wisen.worker.plist)")


def _worker(once: bool) -> None:
    from wisen.cloud.client import Credentials, credentials_path
    from wisen.cloud.worker import run, run_local
    from wisen.store import get_store

    creds = Credentials.load()
    if creds is None:
        if once:
            raise SystemExit(f"not paired ({credentials_path()} missing): run wisen cloud pair https://<server>")
        print("wisen worker: not paired; running local jobs only (Gardener timers, merge checks)")
        asyncio.run(run_local(get_store()))
        return
    print(f"wisen worker: {creds.url} as device {creds.device_id}")
    asyncio.run(run(creds, get_store(), once=once))


async def _gardener(args) -> None:
    """Local-only mode's approval inbox; with a paired server, decide in its
    dashboard instead (the server is the authority for decisions)."""
    from wisen.cloud.worker import drain, tend
    from wisen.gardener.applier import git_row
    from wisen.gardener.loop import on_decided
    from wisen.jobs import get_engine
    from wisen.proposals import IllegalTransition, Proposals
    from wisen.store import get_store
    from wisen.synthesis.job import garden_day

    store, engine = get_store(), get_engine()
    proposals = Proposals(store)
    if args.gardener_cmd == "run":
        day = args.date or (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
        job = await garden_day(day)
        if job.state != "succeeded":
            raise SystemExit(f"gardener failed: {job.error}")
        print(f"{job.result['proposals']} proposals from {day}")
    elif args.gardener_cmd in ("approve", "reject"):
        from wisen.cloud.client import Credentials

        if creds := Credentials.load():
            raise SystemExit(f"paired with {creds.url}: decide in its dashboard, so both sides keep one decision")
        decision = "approved" if args.gardener_cmd == "approve" else "rejected"
        request_id = f"cli:{uuid.uuid4().hex}"
        try:
            d = await proposals.decide(args.id, decision, request_id=request_id, actor="cli",
                                       note=getattr(args, "note", None))
        except IllegalTransition as e:
            raise SystemExit(str(e))
        await on_decided(store, engine, args.id)
        await drain(engine)
        last = (await proposals.history(args.id))[-1]
        print(f"{args.id}: {d.decision}{' earlier' if d.request_id != request_id else ''}; now {last.to_state}"
              + (f": {last.note}" if last.note and last.to_state != d.decision else ""))
    elif args.gardener_cmd == "status":
        await tend(store, engine)
        await drain(engine)
    if args.gardener_cmd in ("list", "status"):
        for p in await proposals.list(states=None if args.all else ("proposed", "approved", "applied",
                                                                    "merged", "measuring")):
            print(f"{p.id}  {p.state:<15} {p.target}")
            if p.state == "applied" and (row := git_row(store, p.id)):
                where = f"git --git-dir {row['git_dir']}" if row["git_dir"] else f"git -C {row['work_tree']}"
                print(f"    branch {row['branch']}; merge it when you choose: {where} merge {row['branch']}")
            if p.state == "proposed":
                print(f"    {p.payload.get('rationale', '')}")
                print(f"    {p.payload.get('eval_summary') or ''}")
                for line in p.payload.get("diff", "").splitlines():
                    print(f"    {line}")


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

    wk = sub.add_parser("worker", help="laptop daemon: with a paired server, sync and run its device jobs; unpaired, run only local jobs (Gardener timers, merge checks)")
    wk.add_argument("--once", action="store_true", help="one pass, no waiting (for debugging)")

    gd = sub.add_parser("gardener", help="Gardener proposals (local-only mode's approval inbox)")
    gs = gd.add_subparsers(dest="gardener_cmd", required=True)
    c = gs.add_parser("run", help="run the Gardener for a day now (default: yesterday)")
    c.add_argument("date", nargs="?")
    c = gs.add_parser("list", help="open proposals with their diffs")
    c.add_argument("--all", action="store_true", help="every proposal, in any state")
    c = gs.add_parser("status", help="check applied branches for merges, run due jobs, then list")
    c.add_argument("--all", action="store_true", help="every proposal, in any state")
    c = gs.add_parser("approve", help="approve a proposal; its branch is made at once")
    c.add_argument("id")
    c = gs.add_parser("reject", help="reject a proposal; the note reaches the Gardener")
    c.add_argument("id")
    c.add_argument("--note", help="why, in your words")

    cl = sub.add_parser("cloud", help="the wisen server, and pairing with one")
    cs = cl.add_subparsers(dest="cloud_cmd", required=True)
    db_help = "server SQLite file (default: WISEN_CLOUD_DB or ~/.wisen/cloud/wisen.db)"
    c = cs.add_parser("serve", help="run the server (one process, one worker)")
    c.add_argument("--host", default="127.0.0.1")
    c.add_argument("--port", type=int, default=8722)
    c.add_argument("--db", help=db_help)
    c = cs.add_parser("user", help="create a login, or set a new password (prompts)")
    c.add_argument("name")
    c.add_argument("--timezone", default="UTC", help="IANA name for the daily run, e.g. Europe/Berlin")
    c.add_argument("--daily-at", default="07:00", help="HH:MM local time of the daily synthesis (default 07:00)")
    c.add_argument("--db", help=db_help)
    cs.add_parser("vapid-keys", help="print a new Web Push key pair as env lines")
    c = cs.add_parser("backup", help="consistent snapshot of the server database")
    c.add_argument("dir", help="directory for wisen-<time>.db snapshots")
    c.add_argument("--keep", type=int, default=14, help="snapshots to keep (default 14)")
    c.add_argument("--db", help=db_help)
    c = cs.add_parser("restore", help="put a snapshot back (stop the server first)")
    c.add_argument("snapshot")
    c.add_argument("--db", help=db_help)
    c = cs.add_parser("pair", help="pair this laptop with a server")
    c.add_argument("url", help="e.g. https://wisen.example.org")
    c.add_argument("--code", help="one-time code from the dashboard (prompted if omitted)")
    c.add_argument("--name", help="device name shown in the dashboard (default: host name)")

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
    elif args.cmd == "worker":
        _worker(args.once)
    elif args.cmd == "cloud":
        _cloud(args)
    elif args.cmd == "gardener":
        asyncio.run(_gardener(args))
    elif args.cmd == "eval":
        from wisen.evals.cli import main as eval_main
        sys.argv = ["wisen eval"] + args.args
        eval_main()
