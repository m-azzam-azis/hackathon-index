"""Devpost crawl stages. Each stage is resumable: cached pages are never re-fetched.

    list      every ended hackathon from the listing JSON (cheap: 40 per request)
    overview  prize blocks + judges, for in-window events with at least one cash prize
    gallery   every submission card, for events whose estimated cash clears the bar
    projects  full project pages: all winners + a random sample of non-winners
"""

import json
import random
import sqlite3

from ...db import upsert
from ...fetcher import Fetcher
from . import parse

SOURCE = parse.SOURCE
LIST_URL = "https://devpost.com/api/hackathons?status[]=ended&per_page=40&page={page}"  # 40 is the max


def crawl_list(conn: sqlite3.Connection, fetcher: Fetcher, since: str, max_pages: int | None = None) -> None:
    page, kept = 1, 0
    while max_pages is None or page <= max_pages:
        # The first pages change as events end, so always refresh page 1..3.
        body = fetcher.get(LIST_URL.format(page=page), SOURCE, "list", refresh=page <= 3)
        if body is None:
            print(f"  list page {page}: no body, stopping")
            break
        items = json.loads(body).get("hackathons") or []
        if not items:
            break
        for h in items:
            row = parse.parse_listing_item(h)
            if row["end_date"] and row["end_date"] >= since:
                row["raw"] = json.dumps(h)
                upsert(conn, "hackathons", row, ("source", "source_id"))
                kept += 1
        conn.commit()
        if page % 25 == 0:
            print(f"  list page {page}: {kept} in window so far", flush=True)
        page += 1
    print(f"list: {kept} hackathons in window (since {since}) across {page - 1} pages")


def _targets(conn, where: str, limit: int | None, params=()) -> list[sqlite3.Row]:
    # Events with winners first, so a --limit pilot reaches the gallery stage.
    sql = f"SELECT * FROM hackathons WHERE source=? AND {where} ORDER BY winners_announced DESC, end_date DESC"
    rows = conn.execute(sql, (SOURCE, *params)).fetchall()
    return rows[:limit] if limit else rows


def crawl_overviews(conn, fetcher: Fetcher, limit: int | None = None, reparse: bool = False) -> None:
    # Loose on purpose: any cash prize, or a headline amount we couldn't classify.
    where = "(cash_prizes_count > 0 OR headline_prize_usd > 0) AND invite_only = 0"
    rows = _targets(conn, where if reparse else f"{where} AND cash_usd_est IS NULL", limit)
    print(f"overview: {len(rows)} hackathons")
    for i, h in enumerate(rows, 1):
        html = fetcher.get(h["url"], SOURCE, "overview")
        if html is None:
            continue
        data = parse.parse_overview(html)
        for p in data["prizes"]:
            upsert(conn, "prizes", {"hackathon_id": h["id"], **p}, ("hackathon_id", "position"))
        for j in data["judges"]:
            upsert(conn, "judges", {"hackathon_id": h["id"], **j}, ("hackathon_id", "name"))
        conn.execute(
            "UPDATE hackathons SET cash_usd_est=?, cash_confidence=?, judges_count=? WHERE id=?",
            (data["cash_usd_est"], data["cash_confidence"], len(data["judges"]), h["id"]),
        )
        conn.commit()
        if i % 20 == 0:
            print(f"  overview {i}/{len(rows)}", flush=True)


def crawl_galleries(conn, fetcher: Fetcher, min_cash: float, limit: int | None = None,
                    reparse: bool = False) -> None:
    # Low-confidence estimates also qualify if the headline clears the bar; the LLM pass
    # over prize text decides later, and we'd rather have the data than miss the event.
    rows = _targets(
        conn,
        "winners_announced = 1 AND (cash_usd_est >= ? OR (cash_confidence = 'low' AND headline_prize_usd >= ?))"
        + ("" if reparse else " AND submissions_count IS NULL"),
        limit, (min_cash, min_cash),
    )
    print(f"gallery: {len(rows)} hackathons (min cash ${min_cash:,.0f})")
    for i, h in enumerate(rows, 1):
        rank, page, complete = 0, 1, False
        while True:
            html = fetcher.get(f"{h['url']}project-gallery?page={page}", SOURCE, "gallery")
            if html is None:
                break  # failed page: leave submissions_count NULL so the next run retries
            data = parse.parse_gallery(html)
            for e in data["entries"]:
                rank += 1
                pid = upsert(conn, "projects", {
                    "source": SOURCE, "source_id": e["source_id"], "url": e["url"],
                    "title": e["title"], "tagline": e["tagline"],
                    "likes": e["likes"], "comments": e["comments"], "team_size": len(e["members"]) or None,
                }, ("source", "url"))
                upsert(conn, "submissions", {"hackathon_id": h["id"], "project_id": pid,
                                             "gallery_rank": rank, "is_winner": e["is_winner"]},
                       ("hackathon_id", "project_id"))
                _link_members(conn, pid, e["members"])
            if not data["has_next"] or not data["entries"]:
                complete = True
                break
            page += 1
        if complete:
            conn.execute("UPDATE hackathons SET submissions_count=? WHERE id=?", (rank, h["id"]))
        conn.commit()
        print(f"  gallery {i}/{len(rows)} {h['title']}: {rank} submissions", flush=True)


def crawl_projects(conn, fetcher: Fetcher, loser_rate: float, min_losers: int,
                   limit: int | None = None, seed: int = 42, reparse: bool = False) -> None:
    hackathons = _targets(conn, "submissions_count > 0", limit)
    rng = random.Random(seed)  # same sample on every run, so resuming is stable
    todo: list[int] = []
    for h in hackathons:
        subs = conn.execute(
            "SELECT project_id, is_winner FROM submissions WHERE hackathon_id=? ORDER BY gallery_rank",
            (h["id"],)).fetchall()
        winners = [s["project_id"] for s in subs if s["is_winner"]]
        losers = [s["project_id"] for s in subs if not s["is_winner"]]
        k = min(len(losers), max(min_losers, round(len(losers) * loser_rate)))
        todo += winners + rng.sample(losers, k)

    seen, ordered = set(), []
    for pid in todo:
        if pid not in seen:
            seen.add(pid)
            ordered.append(pid)
    if not reparse:
        done = {r[0] for r in conn.execute("SELECT id FROM projects WHERE detail_fetched = 1")}
        ordered = [pid for pid in ordered if pid not in done]
    print(f"projects: {len(ordered)} project pages to go")

    by_host = {parse.hackathon_key(r["url"]): r["id"] for r in conn.execute(
        "SELECT id, url FROM hackathons WHERE source=?", (SOURCE,))}
    for i, pid in enumerate(ordered, 1):
        url = conn.execute("SELECT url FROM projects WHERE id=?", (pid,)).fetchone()["url"]
        html = fetcher.get(url, SOURCE, "project")
        if html is None:
            continue
        d = parse.parse_project(html)
        conn.execute(
            "UPDATE projects SET title=?, tagline=COALESCE(?, tagline), likes=COALESCE(?, likes), "
            "built_with=?, links=?, has_repo=?, has_video=?, image_count=?, writeup=?, writeup_chars=?, "
            "team_size=COALESCE(?, team_size), detail_fetched=1 WHERE id=?",
            (d["title"], d["tagline"], d["likes"], json.dumps(d["built_with"]), json.dumps(d["links"]),
             d["has_repo"], d["has_video"], d["image_count"], json.dumps(d["writeup"]),
             d["writeup_chars"], len(d["members"]) or None, pid),
        )
        _link_members(conn, pid, d["members"])
        # Cross-submissions: the same project entered elsewhere (and maybe won there).
        for s in d["submitted_to"]:
            hid = by_host.get(parse.hackathon_key(s["hackathon_url"]))
            if hid is None:
                continue  # outside our window / filters
            conn.execute("INSERT OR IGNORE INTO submissions (hackathon_id, project_id, is_winner) "
                         "VALUES (?, ?, ?)", (hid, pid, int(bool(s["prizes"]))))
            for prize in s["prizes"]:
                conn.execute("INSERT OR IGNORE INTO submission_prizes VALUES (?, ?, ?)", (hid, pid, prize))
        conn.commit()
        if i % 25 == 0:
            print(f"  projects {i}/{len(ordered)}", flush=True)


def _link_members(conn, project_id: int, profile_urls: list[str]) -> None:
    for url in profile_urls:
        person = upsert(conn, "people", {"source": SOURCE, "profile_url": url,
                                         "username": url.rstrip("/").rsplit("/", 1)[-1]},
                        ("source", "profile_url"))
        conn.execute("INSERT OR IGNORE INTO project_members VALUES (?, ?)", (project_id, person))
