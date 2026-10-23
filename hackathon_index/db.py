"""SQLite schema. Source-agnostic: every row carries `source` (devpost, lablab, offline, ...)."""

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    url         TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- list | overview | gallery | project
    status      INTEGER,
    fetched_at  TEXT,
    path        TEXT,                   -- gzipped raw body on disk
    error       TEXT
);

CREATE TABLE IF NOT EXISTS hackathons (
    id                  INTEGER PRIMARY KEY,
    source              TEXT NOT NULL,
    source_id           TEXT NOT NULL,
    url                 TEXT NOT NULL,
    title               TEXT,
    organizer           TEXT,
    start_date          TEXT,
    end_date            TEXT,
    location            TEXT,
    is_online           INTEGER,
    invite_only         INTEGER,
    managed_by_source   INTEGER,        -- "Managed by Devpost" badge
    winners_announced   INTEGER,
    themes              TEXT,           -- JSON list
    registrants         INTEGER,
    submissions_count   INTEGER,        -- filled from the gallery
    headline_prize_text TEXT,
    headline_currency   TEXT,
    headline_prize_usd  REAL,
    cash_prizes_count   INTEGER,
    other_prizes_count  INTEGER,
    judges_count        INTEGER,
    categories_done     INTEGER,        -- categories recorded; NULL = category pass not done
    cash_usd_est        REAL,           -- rough, from prize blocks; refined later
    cash_confidence     TEXT,           -- high | low
    raw                 TEXT,           -- listing JSON
    UNIQUE (source, source_id)
);

CREATE TABLE IF NOT EXISTS prizes (
    id                INTEGER PRIMARY KEY,
    hackathon_id      INTEGER NOT NULL REFERENCES hackathons(id),
    source_prize_id   TEXT,
    position          INTEGER,
    title             TEXT,
    value_text        TEXT,
    value_amount      REAL,
    value_currency    TEXT,
    cash_labeled      INTEGER,          -- value says "in cash"
    winners_count     INTEGER,
    description       TEXT,
    kind              TEXT,             -- cash | credits | swag | mixed (later, LLM pass)
    cash_usd          REAL,             -- later, LLM pass
    UNIQUE (hackathon_id, position)
);

CREATE TABLE IF NOT EXISTS judges (
    hackathon_id  INTEGER NOT NULL REFERENCES hackathons(id),
    name          TEXT,
    title         TEXT,
    UNIQUE (hackathon_id, name)
);

CREATE TABLE IF NOT EXISTS projects (
    id              INTEGER PRIMARY KEY,
    source          TEXT NOT NULL,
    source_id       TEXT,
    url             TEXT NOT NULL,
    title           TEXT,
    tagline         TEXT,
    likes           INTEGER,
    comments        INTEGER,
    team_size       INTEGER,
    built_with      TEXT,               -- JSON list
    links           TEXT,               -- JSON list
    has_repo        INTEGER,
    has_video       INTEGER,
    image_count     INTEGER,
    writeup         TEXT,               -- JSON {section heading: text}
    writeup_chars   INTEGER,
    detail_fetched  INTEGER DEFAULT 0,
    UNIQUE (source, url)
);

CREATE TABLE IF NOT EXISTS submissions (
    hackathon_id  INTEGER NOT NULL REFERENCES hackathons(id),
    project_id    INTEGER NOT NULL REFERENCES projects(id),
    gallery_rank  INTEGER,
    is_winner     INTEGER,
    PRIMARY KEY (hackathon_id, project_id)
);

-- Which prize a project won, from the project page ("WINNER Best Use of Neo4j").
CREATE TABLE IF NOT EXISTS submission_prizes (
    hackathon_id  INTEGER NOT NULL REFERENCES hackathons(id),
    project_id    INTEGER NOT NULL REFERENCES projects(id),
    prize_title   TEXT NOT NULL,
    PRIMARY KEY (hackathon_id, project_id, prize_title)
);

-- Which submissions entered which prize/category (from per-prize filtered galleries).
CREATE TABLE IF NOT EXISTS submission_categories (
    hackathon_id  INTEGER NOT NULL REFERENCES hackathons(id),
    project_id    INTEGER NOT NULL REFERENCES projects(id),
    prize_id      INTEGER NOT NULL REFERENCES prizes(id),
    PRIMARY KEY (hackathon_id, project_id, prize_id)
);

CREATE TABLE IF NOT EXISTS people (
    id           INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,
    profile_url  TEXT NOT NULL,
    username     TEXT,
    UNIQUE (source, profile_url)
);

CREATE TABLE IF NOT EXISTS project_members (
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    person_id   INTEGER NOT NULL REFERENCES people(id),
    PRIMARY KEY (project_id, person_id)
);
"""


def connect(path: Path, threads: bool = False) -> sqlite3.Connection:
    """`threads=True` for the fetcher's own connection, shared by worker threads under its lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=not threads)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# Columns added after the first release; ALTER TABLE for databases created before them.
_ADDED_COLUMNS = {"hackathons": {"categories_done": "INTEGER"}}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in _ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, kind in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")
    conn.commit()


def upsert(conn: sqlite3.Connection, table: str, row: dict, keys: tuple[str, ...]) -> int:
    """Insert or update on the unique `keys`; returns the row id (or rowid)."""
    row = {k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in row.items()}
    cols = ", ".join(row)
    marks = ", ".join(f":{c}" for c in row)
    updates = ", ".join(f"{c}=excluded.{c}" for c in row if c not in keys) or f"{keys[0]}={keys[0]}"
    conn.execute(
        f"INSERT INTO {table} ({cols}) VALUES ({marks}) "
        f"ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {updates}",
        row,
    )
    where = " AND ".join(f"{k}=:{k}" for k in keys)
    return conn.execute(f"SELECT rowid FROM {table} WHERE {where}", row).fetchone()[0]
