"""Devpost crawl stages. Each stage is resumable: cached pages are never re-fetched.

    list      every ended hackathon from the listing JSON (cheap: 40 per request)
    overview  prize blocks + judges, for in-window events with at least one cash prize
    gallery     every submission card, for events whose estimated cash clears the bar
    categories  which submissions entered which prize/category (per-prize filtered galleries)
    projects    full project pages: all winners + a sample of non-winners, balanced across categories

Stages after `list` run items in parallel when the fetcher has workers > 1: threads only
fetch and parse; every database write happens on the calling thread.
"""

import json
import random
import sqlite3
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, timedelta

from ...db import upsert
from ...fetcher import Fetcher
from ...progress import Progress
from . import parse

SOURCE = parse.SOURCE
LIST_URL = "https://devpost.com/api/hackathons?status[]=ended&per_page=40&page={page}"  # 40 is the max


def crawl_list(conn: sqlite3.Connection, fetcher: Fetcher, since: str, max_pages: int | None = None,
               refresh_days: int = 60) -> None:
    """Walk every listing page. Pages are cached, but the front of the list shifts as events
    end and winners get announced, so pages are re-fetched for as long as they still hold
    events that ended within `refresh_days` (the listing is roughly newest first)."""
    recent_cutoff = (date.today() - timedelta(days=refresh_days)).isoformat()
    page, kept, prog, refreshing = 1, 0, None, True
    while max_pages is None or page <= max_pages:
        body = fetcher.get(LIST_URL.format(page=page), SOURCE, "list", refresh=refreshing)
        if body is None:
            print(f"  list page {page}: no body, stopping")
            break
        data = json.loads(body)
        items = data.get("hackathons") or []
        if prog is None:
            pages = -(-(data.get("meta") or {}).get("total_count", 0) // 40)
            prog = Progress("list", min(pages, max_pages or pages), fetcher)
        if not items:
            break
        rows = [parse.parse_listing_item(h) for h in items]
        refreshing = refreshing and any(r["end_date"] and r["end_date"] >= recent_cutoff for r in rows)
        for h, row in zip(items, rows):
            if row["end_date"] and row["end_date"] >= since:
                row["raw"] = json.dumps(h)
                upsert(conn, "hackathons", row, ("source", "source_id"))
                kept += 1
        conn.commit()
        prog.step(f"{kept} in window")
        page += 1
    if prog:
        prog.done(f"{kept} hackathons since {since}")


def _targets(conn, where: str, limit: int | None, params=()) -> list[sqlite3.Row]:
    # Events with winners first, so a --limit pilot reaches the gallery stage.
    sql = f"SELECT * FROM hackathons WHERE source=? AND {where} ORDER BY winners_announced DESC, end_date DESC"
    rows = conn.execute(sql, (SOURCE, *params)).fetchall()
    return rows[:limit] if limit else rows


def _parallel(items, fn, fetcher):
    """Yield (item, fn(item)) as they finish, running up to fetcher.workers at once."""
    workers = getattr(fetcher, "workers", 1)
    if workers <= 1:
        for item in items:
            yield item, fn(item)
        return
    pool = ThreadPoolExecutor(workers)
    todo, pending, finished = iter(items), set(), False

    def submit():
        for item in todo:
            pending.add(pool.submit(lambda x=item: (x, fn(x))))
            return

    try:
        for _ in range(workers * 2):
            submit()
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                pending.discard(fut)
                yield fut.result()  # re-raises CrawlHalted from a worker
                submit()
        finished = True
    finally:
        if not finished:  # Ctrl-C, halt, or an error: stop workers mid-backoff
            fetcher.stop()
        pool.shutdown(wait=True, cancel_futures=True)


# ---------------------------------------------------------------- overview

def crawl_overviews(conn, fetcher: Fetcher, limit: int | None = None, reparse: bool = False) -> None:
    # Loose on purpose: any cash prize, or a headline amount we couldn't classify.
    where = "(cash_prizes_count > 0 OR headline_prize_usd > 0) AND invite_only = 0"
    rows = _targets(conn, where if reparse else f"{where} AND cash_usd_est IS NULL", limit)
    prog = Progress("overview", len(rows), fetcher)

    def work(h):
        html = fetcher.get(h["url"], SOURCE, "overview")
        return parse.parse_overview(html) if html is not None else None

    for h, data in _parallel(rows, work, fetcher):
        prog.step(h["title"] or "")
        if data is None:
            continue
        for p in data["prizes"]:
            upsert(conn, "prizes", {"hackathon_id": h["id"], **p}, ("hackathon_id", "position"))
        for j in data["judges"]:
            upsert(conn, "judges", {"hackathon_id": h["id"], **j}, ("hackathon_id", "name"))
        conn.execute(
            "UPDATE hackathons SET cash_usd_est=?, cash_confidence=?, judges_count=? WHERE id=?",
            (data["cash_usd_est"], data["cash_confidence"], len(data["judges"]), h["id"]),
        )
        conn.commit()
    prog.done()


# ---------------------------------------------------------------- gallery

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
    prog = Progress("gallery", len(rows), fetcher)

    def work(h):
        entries, page = [], 1
        while True:
            html = fetcher.get(f"{h['url']}project-gallery?page={page}", SOURCE, "gallery")
            if html is None:
                return entries, False  # failed page: leave submissions_count NULL so the next run retries
            data = parse.parse_gallery(html)
            entries += data["entries"]
            if not data["has_next"] or not data["entries"]:
                return entries, True
            page += 1
            if page % 5 == 0:
                prog.update(f"{h['title']}: page {page}, {len(entries)} submissions so far")

    for h, (entries, complete) in _parallel(rows, work, fetcher):
        for rank, e in enumerate(entries, 1):
            pid = upsert(conn, "projects", {
                "source": SOURCE, "source_id": e["source_id"], "url": e["url"],
                "title": e["title"], "tagline": e["tagline"],
                "likes": e["likes"], "comments": e["comments"], "team_size": len(e["members"]) or None,
            }, ("source", "url"))
            upsert(conn, "submissions", {"hackathon_id": h["id"], "project_id": pid,
                                         "gallery_rank": rank, "is_winner": e["is_winner"]},
                   ("hackathon_id", "project_id"))
            _link_members(conn, pid, e["members"])
        if complete:
            conn.execute("UPDATE hackathons SET submissions_count=? WHERE id=?", (len(entries), h["id"]))
        conn.commit()
        prog.step(f"{h['title']}: {len(entries)} submissions")
    prog.done()


# ---------------------------------------------------------------- categories

def category_url(hackathon_url: str, prize_id: str, page: int) -> str:
    # The gallery's filter form submits here (GET); same card markup as the gallery.
    return f"{hackathon_url}submissions/search?prize_filter[prizes][]={prize_id}&page={page}"


def crawl_categories(conn, fetcher: Fetcher, min_submissions: int, max_pages: int = 3,
                     limit: int | None = None, reparse: bool = False) -> None:
    """For events whose gallery has a 'filter by prize' control, record which submissions
    entered each prize/category. Only the first `max_pages` pages per category (24 each)
    are read: enough candidates to sample losers from, without crawling huge events twice."""
    rows = _targets(conn, "submissions_count >= ?" + ("" if reparse else " AND categories_done IS NULL"),
                    limit, (max(1, min_submissions),))
    prog = Progress("categories", len(rows), fetcher)

    def work(h):
        first = fetcher.get(f"{h['url']}project-gallery?page=1", SOURCE, "gallery")
        if first is None:
            return {}, False
        found, complete = {}, True
        for prize_id in parse.parse_gallery(first)["prize_filter_ids"]:
            urls = []
            for page in range(1, max_pages + 1):
                html = fetcher.get(category_url(h["url"], prize_id, page), SOURCE, "category")
                if html is None:
                    complete = False
                    break
                data = parse.parse_gallery(html)
                urls += [e["url"] for e in data["entries"]]
                if not data["has_next"] or not data["entries"]:
                    break
            found[prize_id] = urls
        return found, complete

    for h, (found, complete) in _parallel(rows, work, fetcher):
        # If every category returns the same list, the filter was ignored: store nothing.
        lists = [tuple(v) for v in found.values()]
        ignored = len(lists) > 1 and len(set(lists)) == 1
        stored = 0
        if not ignored:
            prize_ids = {r["source_prize_id"]: r["id"] for r in conn.execute(
                "SELECT id, source_prize_id FROM prizes WHERE hackathon_id=?", (h["id"],))}
            for source_prize_id, urls in found.items():
                prize_row = prize_ids.get(source_prize_id)
                if prize_row is None or not urls:
                    continue
                stored += 1
                for url in urls:
                    pid = conn.execute("SELECT id FROM projects WHERE source=? AND url=?",
                                       (SOURCE, url)).fetchone()
                    if pid:
                        conn.execute("INSERT OR IGNORE INTO submission_categories VALUES (?, ?, ?)",
                                     (h["id"], pid[0], prize_row))
        if complete:
            conn.execute("UPDATE hackathons SET categories_done=? WHERE id=?", (stored, h["id"]))
        conn.commit()
        prog.step(f"{h['title']}: {stored} categories")
    prog.done()


# ---------------------------------------------------------------- projects

def pick_losers(conn, h, loser_rate: float, min_losers: int, max_losers: int, top_liked: int,
                seed: int = 42) -> list[int]:
    """Deterministic, prefix-stable non-winner sample for one event, in priority order:
    the most-liked (near-miss candidates), then round-robin across categories so every
    category is represented, then a seeded random fill. Same event -> same list; other
    events don't affect it; raising the cap only appends. Ordering keys are project URLs,
    not database ids, so the sample is the same even in a database rebuilt from scratch."""
    if loser_rate <= 0 and min_losers <= 0:
        return []
    subs = conn.execute(
        "SELECT s.project_id, p.url, COALESCE(p.likes, 0) AS likes FROM submissions s "
        "JOIN projects p ON p.id = s.project_id WHERE s.hackathon_id=? AND s.is_winner = 0",
        (h["id"],)).fetchall()
    k = min(len(subs), max_losers, max(min_losers, round(len(subs) * loser_rate)))
    id_of = {s["url"]: s["project_id"] for s in subs}
    rng = lambda tag: random.Random(f"{seed}:{h['source']}:{h['source_id']}:{tag}")

    liked = [s["url"] for s in sorted(subs, key=lambda s: (-s["likes"], s["url"]))[:top_liked]]

    by_cat: dict[str, list[str]] = {}
    for prize_key, url in conn.execute(
            "SELECT COALESCE(z.source_prize_id, z.title), p.url FROM submission_categories c "
            "JOIN prizes z ON z.id = c.prize_id JOIN projects p ON p.id = c.project_id "
            "WHERE c.hackathon_id=? ORDER BY z.position, p.url", (h["id"],)):
        if url in id_of:
            by_cat.setdefault(prize_key, []).append(url)
    for prize_key, urls in by_cat.items():
        rng(f"cat:{prize_key}").shuffle(urls)
    round_robin = [urls[i] for i in range(max(map(len, by_cat.values()), default=0))
                   for urls in by_cat.values() if i < len(urls)]

    rest = sorted(id_of)
    rng("rest").shuffle(rest)

    ordered = list(dict.fromkeys(liked + round_robin + rest))
    return [id_of[u] for u in ordered[:k]]


def crawl_projects(conn, fetcher: Fetcher, loser_rate: float, min_losers: int, max_losers: int = 40,
                   top_liked: int = 10, limit: int | None = None, seed: int = 42,
                   reparse: bool = False, min_submissions: int = 0) -> None:
    """All winners, plus `pick_losers` for events with at least `min_submissions`."""
    hackathons = _targets(conn, "submissions_count > 0", limit)
    todo: list[int] = []
    for h in hackathons:
        todo += [r[0] for r in conn.execute(
            "SELECT project_id FROM submissions WHERE hackathon_id=? AND is_winner=1 ORDER BY gallery_rank",
            (h["id"],))]
        if (h["submissions_count"] or 0) >= min_submissions:
            todo += pick_losers(conn, h, loser_rate, min_losers, max_losers, top_liked, seed)

    seen, ordered = set(), []
    for pid in todo:
        if pid not in seen:
            seen.add(pid)
            ordered.append(pid)
    if not reparse:
        done = {r[0] for r in conn.execute("SELECT id FROM projects WHERE detail_fetched = 1")}
        ordered = [pid for pid in ordered if pid not in done]
    urls = {pid: conn.execute("SELECT url FROM projects WHERE id=?", (pid,)).fetchone()["url"] for pid in ordered}
    prog = Progress("projects", len(ordered), fetcher)

    def work(pid):
        html = fetcher.get(urls[pid], SOURCE, "project")
        return parse.parse_project(html) if html is not None else None

    by_host = {parse.hackathon_key(r["url"]): r["id"] for r in conn.execute(
        "SELECT id, url FROM hackathons WHERE source=?", (SOURCE,))}
    for pid, d in _parallel(ordered, work, fetcher):
        prog.step(urls[pid].rsplit("/", 1)[-1])
        if d is None:
            continue
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
    prog.done()


def _link_members(conn, project_id: int, profile_urls: list[str]) -> None:
    for url in profile_urls:
        person = upsert(conn, "people", {"source": SOURCE, "profile_url": url,
                                         "username": url.rstrip("/").rsplit("/", 1)[-1]},
                        ("source", "profile_url"))
        conn.execute("INSERT OR IGNORE INTO project_members VALUES (?, ?)", (project_id, person))
