"""data/explore.db: a flat, read-only copy of the analysis set for the dashboard and for Claude.

Rebuilt from index.db in a few seconds whenever index.db is newer, so it never holds anything
that can't be regenerated. Tables:

    events     one row per event with its LLM grade, prize split and scores
    projects   every tagged project (A/B events) with its event, tags, scores, links and team
    builders   Devpost profiles with their winning projects
    traits     win-rate gaps per trait, measured within each event
    projects_fts  full-text search over title, tagline, summary and built-with

Non-winners are a capped sample, so raw win rates overstate everyone's chances. Compare
winners with non-winners from the same event, as `traits` does.
"""

import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = re.compile(r"github\.com|gitlab\.com|bitbucket\.org")
CATEGORIES = ("domain", "ai_role", "form", "sponsor_tech")
FLAGS = {
    "specific_user": lambda r: r["specific_user"] == 1,
    "real_use": lambda r: r["real_use"] == 1,
    "has_repo": lambda r: r["has_repo"] == 1,
    "has_video": lambda r: r["has_video"] == 1,
    "solo": lambda r: r["team_size"] == 1,
    "team of 4+": lambda r: (r["team_size"] or 0) >= 4,
    "writeup over 6k chars": lambda r: (r["writeup_chars"] or 0) > 6000,
    "writeup under 2k chars": lambda r: (r["writeup_chars"] or 0) < 2000,
    "originality 8+": lambda r: r["originality"] >= 8,
    "technical depth 8+": lambda r: r["technical_depth"] >= 8,
    "polish 8+": lambda r: r["polish"] >= 8,
}
MIN_TECH = 80   # a built-with technology needs this many projects to get its own trait row


def path(data: Path) -> Path:
    return data / "explore.db"


def stale(data: Path) -> bool:
    out, src = path(data), data / "index.db"
    if not out.exists():
        return True
    newest = max(p.stat().st_mtime for p in (src, Path(f"{src}-wal")) if p.exists())
    return newest > out.stat().st_mtime


def build(data: Path, log=print) -> Path:
    """Write explore.db next to index.db (via a temp file, so readers never see half a build)."""
    src = sqlite3.connect(f"file:{data / 'index.db'}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    tmp = data / "explore.db.tmp"
    tmp.unlink(missing_ok=True)
    out = sqlite3.connect(tmp)
    out.executescript(SCHEMA)
    _events(src, out)
    projects = _projects(src, out)
    _builders(src, out)
    _traits(out, projects)
    out.execute("INSERT INTO projects_fts(projects_fts) VALUES ('rebuild')")
    out.commit()
    out.close()
    src.close()
    os.replace(tmp, path(data))
    log(f"explore.db: {len(projects):,} projects, "
        f"{sum(1 for p in projects if p['won']):,} winners")
    return path(data)


def ensure(data: Path) -> Path:
    if stale(data):
        build(data, log=lambda m: print(m, file=sys.stderr))
    return path(data)


SCHEMA = """
CREATE TABLE events (
    id INTEGER PRIMARY KEY, title TEXT, url TEXT, organizer TEXT, year INTEGER, end_date TEXT,
    online INTEGER, registrants INTEGER, submissions INTEGER, grade TEXT, quality INTEGER,
    event_type TEXT, cash_usd REAL, products_usd REAL, credits_usd REAL, stakes INTEGER,
    sponsor_caliber INTEGER, competition INTEGER, tier TEXT, grade_reason TEXT, themes TEXT,
    tagged_projects INTEGER, tagged_winners INTEGER
);
CREATE TABLE projects (
    project_id INTEGER PRIMARY KEY, title TEXT, url TEXT, tagline TEXT, summary TEXT,
    won INTEGER, prizes TEXT, event TEXT, event_url TEXT, event_id INTEGER, grade TEXT, year INTEGER,
    domain TEXT, ai_role TEXT, form TEXT, sponsor_tech TEXT,
    originality INTEGER, technical_depth INTEGER, polish INTEGER, combined INTEGER,
    batch_rank INTEGER, batch_size INTEGER, specific_user INTEGER, real_use INTEGER,
    has_repo INTEGER, repo_url TEXT, has_video INTEGER, video_url TEXT, team_size INTEGER,
    likes INTEGER, writeup_chars INTEGER, built_with TEXT, members TEXT
);
CREATE INDEX projects_event ON projects(event_id);
CREATE TABLE builders (
    profile_url TEXT PRIMARY KEY, username TEXT, wins_ab INTEGER, wins_all INTEGER,
    projects_fetched INTEGER, events INTEGER, winning_projects TEXT
);
CREATE TABLE traits (
    scope TEXT, trait TEXT, value TEXT, projects INTEGER, winner_rate REAL, nonwinner_rate REAL,
    within_event_gap REAL, events INTEGER
);
CREATE VIRTUAL TABLE projects_fts USING fts5(
    title, tagline, summary, built_with, content='projects', content_rowid='project_id'
);
"""


def _events(src, out):
    rows = src.execute("""
        SELECT h.id, h.title, h.url, h.organizer, CAST(substr(h.end_date, 1, 4) AS INTEGER), h.end_date,
               h.is_online, h.registrants, h.submissions_count, l.grade, l.quality, l.event_type,
               l.cash_usd, l.products_usd, l.credits_usd, l.stakes, l.sponsor_caliber, l.competition,
               s.tier, l.reason, h.themes,
               (SELECT COUNT(*) FROM project_tags_latest t WHERE t.hackathon_id = h.id),
               (SELECT COUNT(*) FROM project_tags_latest t JOIN submissions su
                  ON su.project_id = t.project_id AND su.hackathon_id = t.hackathon_id
                WHERE t.hackathon_id = h.id AND su.is_winner = 1)
        FROM hackathons h
        LEFT JOIN event_labels_latest l ON l.hackathon_id = h.id
        LEFT JOIN event_scores_latest s ON s.hackathon_id = h.id
        WHERE h.submissions_count > 0""").fetchall()
    for r in rows:
        r = list(r)
        r[20] = ", ".join(json.loads(r[20] or "[]"))
        out.execute(f"INSERT INTO events VALUES ({', '.join('?' * len(r))})", r)


def _projects(src, out) -> list[dict]:
    prizes = defaultdict(list)
    for hid, pid, title in src.execute("SELECT hackathon_id, project_id, prize_title FROM submission_prizes"):
        prizes[(hid, pid)].append(title)
    members = defaultdict(list)
    for pid, url in src.execute("SELECT m.project_id, pe.profile_url FROM project_members m "
                                "JOIN people pe ON pe.id = m.person_id"):
        members[pid].append(url)
    rows = src.execute("""
        SELECT t.*, p.title, p.url, p.tagline, p.links, p.has_repo, p.has_video, p.video_urls,
               p.team_size, p.likes, p.writeup_chars, p.built_with,
               h.title AS event, h.url AS event_url, CAST(substr(h.end_date, 1, 4) AS INTEGER) AS year,
               l.grade, su.is_winner AS won
        FROM project_tags_latest t
        JOIN projects p ON p.id = t.project_id
        JOIN hackathons h ON h.id = t.hackathon_id
        JOIN submissions su ON su.project_id = t.project_id AND su.hackathon_id = t.hackathon_id
        LEFT JOIN event_labels_latest l ON l.hackathon_id = t.hackathon_id""").fetchall()
    kept = []
    for r in rows:
        links = json.loads(r["links"] or "[]")
        videos = json.loads(r["video_urls"] or "[]")
        row = {
            "project_id": r["project_id"], "title": r["title"], "url": r["url"], "tagline": r["tagline"],
            "summary": r["summary"], "won": r["won"] or 0,
            "prizes": "; ".join(prizes[(r["hackathon_id"], r["project_id"])]) or None,
            "event": r["event"], "event_url": r["event_url"], "event_id": r["hackathon_id"],
            "grade": r["grade"], "year": r["year"],
            **{k: r[k] for k in ("domain", "ai_role", "form", "sponsor_tech", "originality",
                                 "technical_depth", "polish", "batch_rank", "batch_size",
                                 "specific_user", "real_use", "has_repo")},
            "combined": (r["originality"] or 0) + (r["technical_depth"] or 0) + (r["polish"] or 0),
            "repo_url": next((x for x in links if REPO.search(x)), None),
            "has_video": r["has_video"], "video_url": videos[0] if videos else None,
            "team_size": r["team_size"], "likes": r["likes"], "writeup_chars": r["writeup_chars"],
            "built_with": ", ".join(json.loads(r["built_with"] or "[]")),
            "members": "\n".join(members[r["project_id"]]) or None,
        }
        out.execute(f"INSERT INTO projects ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                    list(row.values()))
        kept.append(row)
    return kept


def _builders(src, out):
    rows = src.execute("""
        SELECT pe.profile_url, pe.username,
               COUNT(DISTINCT CASE WHEN s.is_winner = 1 AND l.grade IN ('A', 'B') THEN p.id END),
               COUNT(DISTINCT CASE WHEN s.is_winner = 1 THEN p.id END),
               COUNT(DISTINCT p.id), COUNT(DISTINCT s.hackathon_id),
               GROUP_CONCAT(DISTINCT CASE WHEN s.is_winner = 1 THEN p.url END)
        FROM people pe
        JOIN project_members m ON m.person_id = pe.id
        JOIN projects p ON p.id = m.project_id AND p.detail_fetched = 1
        JOIN submissions s ON s.project_id = p.id
        LEFT JOIN event_labels_latest l ON l.hackathon_id = s.hackathon_id
        GROUP BY pe.id HAVING COUNT(DISTINCT CASE WHEN s.is_winner = 1 THEN p.id END) > 0""").fetchall()
    out.executemany("INSERT INTO builders VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(*r[:6], (r[6] or "").replace(",", "\n")) for r in rows])


def within_event(rows: list[dict], has) -> tuple[float, float, float, int, int]:
    """(winner rate, non-winner rate, gap, events, projects with the trait). The gap averages,
    over events with both winners and non-winners, the winners' rate minus the non-winners'
    rate, weighted by the smaller group, so big events and the sampling rate don't skew it."""
    by_event = defaultdict(lambda: ([], []))
    for r in rows:
        by_event[r["event_id"]][0 if r["won"] else 1].append(bool(has(r)))
    won = [x for w, _ in by_event.values() for x in w]
    lost = [x for _, l in by_event.values() for x in l]
    num = den = events = 0
    for w, l in by_event.values():
        if w and l:
            k = min(len(w), len(l))
            num += k * (sum(w) / len(w) - sum(l) / len(l))
            den += k
            events += 1
    rate = lambda xs: sum(xs) / len(xs) if xs else 0.0
    return rate(won), rate(lost), (num / den if den else 0.0), events, sum(won) + sum(lost)


def _traits(out, projects: list[dict]):
    tech = Counter(t.strip().lower() for p in projects for t in set(p["built_with"].split(",")) if t.strip())
    scopes = {"A and B": projects, "A": [p for p in projects if p["grade"] == "A"],
              "B": [p for p in projects if p["grade"] == "B"]}
    for scope, rows in scopes.items():
        traits = [(c, v, lambda r, c=c, v=v: r[c] == v)
                  for c in CATEGORIES for v in sorted({r[c] for r in rows if r[c]})]
        traits += [("flag", name, fn) for name, fn in FLAGS.items()]
        traits += [("built_with", t, lambda r, t=t: t in {x.strip().lower() for x in r["built_with"].split(",")})
                   for t, n in tech.items() if n >= MIN_TECH]
        for trait, value, fn in traits:
            w, l, gap, events, n = within_event(rows, fn)
            if n:
                out.execute("INSERT INTO traits VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (scope, trait, value, n, round(100 * w, 1), round(100 * l, 1),
                             round(100 * gap, 1), events))
