"""
Backups of the server's SQLite file. The file holds packets and results only,
never raw transcripts, so a copy can safely leave the box.

`backup` takes a consistent snapshot with SQLite's online backup API, which is
safe while the server runs and writes (a plain `cp` of a WAL database is not).
`restore` puts a snapshot back; the server must be stopped first.
"""
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

PREFIX = "wisen-"


def _check(path: Path) -> None:
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError(f"{path} failed its integrity check: {result}")


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
    """Replace `db` with `snapshot`. Refuses while the server looks alive (its
    WAL file is non-empty), since swapping a live database corrupts it."""
    _check(snapshot)
    wal = db.with_name(db.name + "-wal")
    if wal.exists() and wal.stat().st_size > 0:
        raise RuntimeError(f"{wal} is not empty: stop the server before restoring")
    db.parent.mkdir(parents=True, exist_ok=True)
    partial = db.with_name(db.name + ".restoring")
    with closing(sqlite3.connect(snapshot)) as source, closing(sqlite3.connect(partial)) as dest:
        source.backup(dest)
    for leftover in (wal, db.with_name(db.name + "-shm")):
        leftover.unlink(missing_ok=True)
    os.replace(partial, db)
