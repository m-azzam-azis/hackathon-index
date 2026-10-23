"""Workers, the category pass, and balanced non-winner picks, against a fake Devpost.

Uses the real Fetcher (own thread-safe connection) over httpx.MockTransport, with sleeps
patched out, so the threading and SQLite paths are the ones that run on a real crawl."""

import json
import re
import threading

import httpx
import pytest

from hackathon_index import fetcher as fetcher_mod
from hackathon_index.db import connect
from hackathon_index.fetcher import CrawlHalted, Fetcher
from hackathon_index.sources.devpost import crawl

N_EVENTS, N_SUBS = 6, 60  # 60 submissions -> 3 gallery pages of 24/24/12
PRIZES = ["501", "502", "503"]  # category ids on every event


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(fetcher_mod.time, "sleep", lambda s: None)


def card(e, i):
    winner = '<aside class="entry-badge"><img class="winner"></aside>' if i < 3 else ""
    return (f'<div class="gallery-item" data-software-id="{e}{i:03d}">'
            f'<a class="block-wrapper-link link-to-software" href="https://devpost.com/software/e{e}-p{i}">'
            f'<h5>P{i}</h5><p class="tagline">t</p>{winner}'
            f'<footer><div class="members"><span data-url="https://devpost.com/u{e}{i}"></span></div>'
            f'<div class="counts"><span class="like-count">{i % 7}</span><span class="comment-count">0</span>'
            f'</div></footer></a></div>')


def gallery(e, ids, page, filters=True):
    per = 24
    chunk = ids[(page - 1) * per: page * per]
    more = page * per < len(ids)
    boxes = "".join(f'<input type="checkbox" name="prize_filter[prizes][]" value="{p}">' for p in PRIZES)
    nxt = '<li class="next next_page "><a>»</a></li>' if more else '<li class="next next_page unavailable"><a>»</a></li>'
    return (f"<html><form>{boxes if filters else ''}</form>{''.join(card(e, i) for i in chunk)}"
            f'<ul class="pagination">{nxt}</ul></html>')


def overview(e):
    blocks = "".join(
        f'<div class="prize" id="prize_{p}"><h6>Category {p}</h6><div class="prize-content">'
        f'<div class="prize-value">$1,000 in cash</div><div class="prize-winners">1 winner</div><p>x</p></div></div>'
        for p in PRIZES)
    return f'<html><div id="prizes">{blocks}</div></html>'


def handler(req: httpx.Request) -> httpx.Response:
    url = str(req.url)
    if "api/hackathons" in url:
        page = int(req.url.params["page"])
        items = [] if page > 1 else [
            {"id": e, "title": f"E{e}", "url": f"https://e{e}.devpost.com/", "winners_announced": True,
             "submission_period_dates": f"Mar 01 - 0{e}, 2025", "prize_amount": "$3,000",
             "prizes_counts": {"cash": 3, "other": 0}, "registrations_count": 500}
            for e in range(1, N_EVENTS + 1)]
        return httpx.Response(200, text=json.dumps({"meta": {"total_count": N_EVENTS}, "hackathons": items}))
    m = re.match(r"https://e(\d+)\.devpost\.com/(.*)", url)
    if m:
        e, rest = int(m.group(1)), m.group(2)
        if not rest:
            return httpx.Response(200, text=overview(e))
        page = int(req.url.params.get("page", 1))
        prize = req.url.params.get("prize_filter[prizes][]")
        if rest.startswith("submissions/search") and prize:  # category k holds i where i % 3 == k
            k = PRIZES.index(prize)
            return httpx.Response(200, text=gallery(e, [i for i in range(N_SUBS) if i % 3 == k], page))
        return httpx.Response(200, text=gallery(e, list(range(N_SUBS)), page))
    if "devpost.com/software/" in url:
        return httpx.Response(200, text=f'<h1 id="app-title">{url.rsplit("/", 1)[1]}</h1>')
    return httpx.Response(404)


def run_all(tmp_path, workers):
    db = tmp_path / "index.db"
    conn = connect(db)
    f = Fetcher(connect(db, threads=True), tmp_path / "raw", min_interval=0, workers=workers)
    f.client = httpx.Client(transport=httpx.MockTransport(handler))
    crawl.crawl_list(conn, f, since="2024-01-01")
    crawl.crawl_overviews(conn, f)
    crawl.crawl_galleries(conn, f, min_cash=1000)
    crawl.crawl_categories(conn, f, min_submissions=10, max_pages=3)
    crawl.crawl_projects(conn, f, loser_rate=0.25, min_losers=5, max_losers=12, top_liked=3,
                         min_submissions=10)
    return conn


def snapshot(conn):
    q = lambda sql: [tuple(r) for r in conn.execute(sql).fetchall()]
    return {
        "subs": q("SELECT h.source_id, COUNT(*) FROM submissions s JOIN hackathons h ON h.id=s.hackathon_id "
                  "GROUP BY 1 ORDER BY 1"),
        "cats": q("SELECT COUNT(*) FROM submission_categories")[0][0],
        "detail": sorted(r[0] for r in q("SELECT url FROM projects WHERE detail_fetched=1")),
    }


def test_workers_give_the_same_result_as_sequential(tmp_path):
    seq = snapshot(run_all(tmp_path / "seq", workers=1))
    par = snapshot(run_all(tmp_path / "par", workers=4))
    assert par == seq
    assert seq["subs"] == [(str(e), N_SUBS) for e in range(1, N_EVENTS + 1)]
    assert seq["cats"] == N_EVENTS * (N_SUBS - 0)  # every submission is in exactly one category
    # 3 winners + 12 losers per event
    assert len(seq["detail"]) == N_EVENTS * 15


def test_losers_cover_every_category(tmp_path):
    conn = run_all(tmp_path, workers=3)
    h = conn.execute("SELECT * FROM hackathons WHERE source_id='1'").fetchone()
    picked = crawl.pick_losers(conn, h, 0.25, 5, 12, top_liked=3)
    cats = {r[0] for r in conn.execute(
        f"SELECT prize_id FROM submission_categories WHERE project_id IN ({','.join('?' * len(picked))})",
        picked)}
    assert len(picked) == 12 and len(cats) == 3
    assert crawl.pick_losers(conn, h, 0.25, 5, 12, top_liked=3) == picked          # deterministic
    assert crawl.pick_losers(conn, h, 0.25, 5, 20, top_liked=3)[:12] == picked     # prefix-stable


def test_resume_makes_no_new_requests(tmp_path):
    run_all(tmp_path, workers=3)
    calls = []
    db = tmp_path / "index.db"
    conn = connect(db)
    f = Fetcher(connect(db, threads=True), tmp_path / "raw", min_interval=0, workers=3)
    f.client = httpx.Client(transport=httpx.MockTransport(lambda r: (calls.append(str(r.url)), handler(r))[1]))
    crawl.crawl_overviews(conn, f)
    crawl.crawl_galleries(conn, f, min_cash=1000)
    crawl.crawl_categories(conn, f, min_submissions=10)
    crawl.crawl_projects(conn, f, loser_rate=0.25, min_losers=5, max_losers=12, top_liked=3, min_submissions=10)
    assert calls == []


def test_halt_stops_all_workers(tmp_path):
    db = tmp_path / "index.db"
    conn = connect(db)
    conn.execute("INSERT INTO hackathons (source, source_id, url, cash_prizes_count, invite_only) "
                 "SELECT 'devpost', value, 'https://b' || value || '.devpost.com/', 1, 0 FROM json_each(?)",
                 (json.dumps([str(i) for i in range(30)]),))
    conn.commit()
    f = Fetcher(connect(db, threads=True), tmp_path / "raw", min_interval=0, workers=4)
    f.client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    with pytest.raises(CrawlHalted):
        crawl.crawl_overviews(conn, f)
    assert threading.active_count() < 3  # pool shut down, no stray workers
