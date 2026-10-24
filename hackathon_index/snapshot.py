"""Full copies of index.db, taken before any step that writes to it, so every earlier
state can be restored with a file copy instead of a re-crawl. Nothing here deletes."""

import sqlite3
from datetime import datetime
from pathlib import Path


def snapshot_dir(db_path: Path) -> Path:
    return db_path.parent / "snapshots"


def take(db_path: Path, label: str) -> Path | None:
    """Copy the database to data/snapshots/<timestamp>-<label>.db (consistent even mid-WAL)."""
    if not db_path.exists():
        return None
    out = snapshot_dir(db_path) / f"{datetime.now():%Y%m%d-%H%M%S}-{label}.db"
    out.parent.mkdir(parents=True, exist_ok=True)
    src, dst = sqlite3.connect(db_path), sqlite3.connect(out)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    print(f"snapshot saved: {out}", flush=True)
    return out


def listing(db_path: Path) -> list[Path]:
    return sorted(snapshot_dir(db_path).glob("*.db"))


def restore(db_path: Path, name: str) -> Path:
    """Make a snapshot the live database. The current state is snapshotted first, so a
    restore can itself be undone."""
    matches = [p for p in listing(db_path) if p.name == name or p.stem == name]
    if not matches:
        raise SystemExit(f"no snapshot named {name!r}; see `snapshots`")
    take(db_path, "before-restore")
    src, dst = sqlite3.connect(matches[0]), sqlite3.connect(db_path)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    return matches[0]
