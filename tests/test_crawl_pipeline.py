"""End-to-end run of the four stages against fixtures, with the network stubbed out."""

import json
from pathlib import Path

from hackathon_index.db import connect
from hackathon_index.sources.devpost import crawl

FIX = Path(__file__).parent / "fixtures"

LISTING = {"hackathons": [
    {"id": 1, "title": "Hypermode KG", "url": "https://hypermode-knowledge-graph-ai.devpost.com/",
     "submission_period_dates": "Dec 16, 2024 - Jan 12, 2025", "prize_amount": "$10,000",
     "prizes_counts": {"cash": 7, "other": 0}, "registrations_count": 507, "winners_announced": True,
     "organization_name": "Hypermode", "displayed_location": {"icon": "globe", "location": "Online"}},
    {"id": 2, "title": "Old one", "url": "https://old.devpost.com/",
     "submission_period_dates": "Mar 01 - 02, 2023", "prize_amount": "$50,000",
     "prizes_counts": {"cash": 3, "other": 0}, "registrations_count": 900, "winners_announced": True},
]}


class StubFetcher:
    def __init__(self):
        self.calls = []

    def get(self, url, source, kind, refresh=False):
        self.calls.append((kind, url))
        if kind == "list":
            return json.dumps(LISTING if url.endswith("page=1") else {"hackathons": []})
        if kind == "overview":
            return (FIX / "overview.html").read_text()
        if kind == "gallery":
            # Page 1 says there's a next page; page 2 is empty.
            return (FIX / "gallery.html").read_text() if url.endswith("page=1") else "<html></html>"
        return (FIX / "project.html").read_text()


def test_pipeline(tmp_path):
    conn = connect(tmp_path / "index.db")
    f = StubFetcher()
    crawl.crawl_list(conn, f, since="2024-01-01")
    crawl.crawl_overviews(conn, f)
    crawl.crawl_galleries(conn, f, min_cash=1000)
    crawl.crawl_projects(conn, f, loser_rate=0.15, min_losers=5)

    q = lambda sql: conn.execute(sql).fetchall()
    assert [r["title"] for r in q("SELECT title FROM hackathons")] == ["Hypermode KG"]  # 2023 dropped
    assert q("SELECT submissions_count, judges_count FROM hackathons")[0][:] == (2, 2)
    assert len(q("SELECT * FROM prizes")) == 4
    assert q("SELECT COUNT(*) FROM projects WHERE detail_fetched = 1")[0][0] == 2
    assert q("SELECT prize_title FROM submission_prizes")[0][0] == "Best Use of Neo4j"
    assert q("SELECT COUNT(*) FROM people")[0][0] == 4


def test_loser_sample_is_capped_and_includes_most_liked(tmp_path):
    conn = connect(tmp_path / "index.db")
    conn.execute("INSERT INTO hackathons (id, source, source_id, url, submissions_count) "
                 "VALUES (1, 'devpost', '1', 'https://big.devpost.com/', 601)")
    for i in range(601):
        conn.execute("INSERT INTO projects (id, source, url, likes) VALUES (?, 'devpost', ?, ?)",
                     (i, f"https://devpost.com/software/p{i}", i))
        conn.execute("INSERT INTO submissions VALUES (1, ?, ?, ?)", (i, i, int(i == 0)))
    fetched = []

    class Recorder:
        def get(self, url, source, kind, refresh=False):
            fetched.append(url)
            return None

    crawl.crawl_projects(conn, Recorder(), loser_rate=0.15, min_losers=5, max_losers=40, top_liked=10)
    assert len(fetched) == 41  # 1 winner + 40 losers, not 90
    most_liked = {f"https://devpost.com/software/p{i}" for i in range(591, 601)}
    assert most_liked <= set(fetched)
