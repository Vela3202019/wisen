"""
Backups of the server's SQLite file. The file holds packets and results only,
never raw transcripts, so a copy can safely leave the box.

`backup` takes a consistent snapshot with SQLite's online backup API, which is
safe while the server runs and writes (a plain `cp` of a WAL database is not).
`restore` puts a snapshot back; the server must be stopped first. A running
server holds an exclusive lock on <db>.lock (`serving`), so restore can tell.
"""
import fcntl
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

PREFIX = "wisen-"


def _check(path: Path) -> None:
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError(f"{path} failed its integrity check: {result}")


def _lock_path(db: Path) -> Path:
    return db.with_name(db.name + ".lock")


@contextmanager
def serving(db: Path):
    """Held by the server for as long as it runs; the OS drops it if the
    process dies."""
    db.parent.mkdir(parents=True, exist_ok=True)
    with open(_lock_path(db), "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"a server is running on {db}: stop it first")
        yield


def backup(db: Path, out_dir: Path, keep: int = 14) -> Path:
    """Snapshot `db` into `out_dir` as wisen-<UTC time>.db, verified, and
    delete all but the newest `keep` snapshots."""
    if not db.exists():
        raise FileNotFoundError(db)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{PREFIX}{datetime.now(UTC):%Y%m%dT%H%M%SZ}.db"
    partial = target.with_suffix(".partial")
    with closing(sqlite3.connect(db)) as source, closing(sqlite3.connect(partial)) as dest:
        source.backup(dest)
    _check(partial)
    os.replace(partial, target)
    for old in sorted(out_dir.glob(f"{PREFIX}*.db"))[:-keep]:
        old.unlink()
    return target


def restore(snapshot: Path, db: Path) -> None:
    """Replace `db` with `snapshot`. Refuses while a server runs on `db`
    (holds its lock), since swapping a live database corrupts it."""
    _check(snapshot)
    with serving(db):
        wal = db.with_name(db.name + "-wal")
        partial = db.with_name(db.name + ".restoring")
        with closing(sqlite3.connect(snapshot)) as source, closing(sqlite3.connect(partial)) as dest:
            source.backup(dest)
        for leftover in (wal, db.with_name(db.name + "-shm")):
            leftover.unlink(missing_ok=True)
        os.replace(partial, db)
