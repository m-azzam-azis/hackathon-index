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
    video_urls      TEXT,               -- JSON list (YouTube/Vimeo watch links)
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

-- Enrichment steps. Each run adds rows under a new run_id and never edits the crawl tables
-- above or earlier runs; the *_latest views pick the newest run.
CREATE TABLE IF NOT EXISTS enrich_runs (
    run_id      TEXT PRIMARY KEY,       -- e.g. score-20261024-053000
    step        TEXT NOT NULL,          -- score | prizes | tags ...
    rules       TEXT,                   -- version of the step's rules
    params      TEXT,                   -- JSON
    snapshot    TEXT,                   -- database copy taken just before the run
    created_at  TEXT,
    finished_at TEXT                    -- NULL while a run can still be resumed
);

CREATE TABLE IF NOT EXISTS event_scores (
    run_id        TEXT NOT NULL REFERENCES enrich_runs(run_id),
    hackathon_id  INTEGER NOT NULL REFERENCES hackathons(id),
    score         INTEGER,              -- sum of flag weights; higher = more doubtful
    tier          TEXT,                 -- ok | watch | junk
    reason        TEXT,                 -- the heaviest flag, e.g. no_cash, tiny_field
    flags         TEXT,                 -- JSON {flag: weight}
    PRIMARY KEY (run_id, hackathon_id)
);

CREATE VIEW IF NOT EXISTS event_scores_latest AS
    SELECT e.* FROM event_scores e
    WHERE e.run_id = (SELECT run_id FROM enrich_runs WHERE step = 'score' ORDER BY created_at DESC LIMIT 1);

-- LLM grades per event (label_events.py): prize value, four 0-10 scores, quality 0-100, grade A-D.
-- Columns added in prompt v2: event_type, stock_prizes, stakes, sponsor_caliber, competition,
-- winner_quality, winner_notes, quality, grade (see _ADDED_COLUMNS).
CREATE TABLE IF NOT EXISTS event_labels (
    run_id                TEXT NOT NULL REFERENCES enrich_runs(run_id),
    hackathon_id          INTEGER NOT NULL REFERENCES hackathons(id),
    cash_usd              REAL,
    gift_cards_usd        REAL,
    credits_usd           REAL,
    products_usd          REAL,
    crypto_tokens_usd     REAL,
    opportunities         TEXT,         -- JSON list
    opportunity_strength  INTEGER,      -- prompt v1 only
    sponsor_known         INTEGER,      -- prompt v1 only
    legit                 INTEGER,      -- 0-100
    inflated              INTEGER,      -- headline padded with credits, swag or per-participant pools
    reason                TEXT,
    verdict               TEXT,         -- worth | maybe | skip (v2: from the grade)
    cost                  REAL,         -- USD for this call
    error                 TEXT,
    PRIMARY KEY (run_id, hackathon_id)
);

CREATE VIEW IF NOT EXISTS event_labels_latest AS
    SELECT l.* FROM event_labels l
    WHERE l.error IS NULL
      AND l.run_id = (SELECT run_id FROM enrich_runs WHERE step = 'events' ORDER BY created_at DESC LIMIT 1);

-- LLM tags per project (tag_projects.py). The model isn't told who won.
CREATE TABLE IF NOT EXISTS project_tags (
    run_id           TEXT NOT NULL REFERENCES enrich_runs(run_id),
    project_id       INTEGER NOT NULL REFERENCES projects(id),
    hackathon_id     INTEGER REFERENCES hackathons(id),   -- the event it was tagged against
    domain           TEXT,          -- health | education | fintech | ... | other
    ai_role          TEXT,          -- none | llm_wrapper | rag | agent | multi_agent | custom_ml | ...
    form             TEXT,          -- web_app | mobile_app | hardware | bot | ...
    sponsor_tech     TEXT,          -- central | passing | none
    originality      INTEGER,       -- 0-10
    technical_depth  INTEGER,       -- 0-10
    polish           INTEGER,       -- 0-10
    specific_user    INTEGER,
    real_use         INTEGER,
    summary          TEXT,
    cost             REAL,
    error            TEXT,
    PRIMARY KEY (run_id, project_id)
);

CREATE VIEW IF NOT EXISTS project_tags_latest AS
    SELECT t.* FROM project_tags t
    WHERE t.error IS NULL
      AND t.run_id = (SELECT run_id FROM enrich_runs WHERE step = 'tags' ORDER BY created_at DESC LIMIT 1);

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
_ADDED_COLUMNS = {
    "hackathons": {"categories_done": "INTEGER"},
    "projects": {"video_urls": "TEXT"},
    "enrich_runs": {"finished_at": "TEXT"},
    "event_labels": {"event_type": "TEXT", "stock_prizes": "INTEGER", "stakes": "INTEGER",
                     "sponsor_caliber": "INTEGER", "competition": "INTEGER", "winner_quality": "INTEGER",
                     "winner_notes": "TEXT", "quality": "INTEGER", "grade": "TEXT"},
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in _ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, kind in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")
    conn.commit()


def new_run_id(conn: sqlite3.Connection, step: str, now) -> str:
    """e.g. score-20261024-053000; suffixed -2, -3 if two runs start within a second."""
    base = run_id = f"{step}-{now:%Y%m%d-%H%M%S}"
    n = 1
    while conn.execute("SELECT 1 FROM enrich_runs WHERE run_id=?", (run_id,)).fetchone():
        n += 1
        run_id = f"{base}-{n}"
    return run_id


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
