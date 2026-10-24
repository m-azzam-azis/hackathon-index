"""Event labeling against a fake OpenAI-compatible API: the sample mode leaves the database
alone, full runs are versioned and resumable, and scoring v2 trusts the labels."""

import json

import httpx

from hackathon_index import label_events as le
from hackathon_index import llm
from hackathon_index.db import connect
from hackathon_index.score import score_events

ANSWERS = {  # by event title
    "Logitech Build": {"cash_usd": 0, "products_usd": 6000, "sponsor_known": 2, "legit": 90,
                       "opportunity_strength": 1, "reason": "real hardware from Logitech"},
    "Google Meetup": {"cash_usd": 0, "products_usd": 0, "opportunities": ["meet the Google Cloud team"],
                      "opportunity_strength": 3, "sponsor_known": 2, "legit": 85, "reason": "strong opportunity"},
    "Bogus Fund": {"cash_usd": 10000, "sponsor_known": 0, "legit": 20, "inflated": True, "reason": "unverifiable"},
}


def fake_api(calls):
    def handler(request):
        body = json.loads(request.content)
        title = body["messages"][1]["content"].split("\n")[0].removeprefix("Title: ")
        calls.append(title)
        answer = "```json\n" + json.dumps(ANSWERS[title]) + "\n```"   # fenced, as some models do
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}}],
                                         "usage": {"cost": 0.001}})
    return httpx.MockTransport(handler)


def setup(tmp_path, monkeypatch, calls):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    original = llm.Client.__init__
    monkeypatch.setattr(le.Client, "__init__",
                        lambda self, model, **kw: original(self, model, transport=fake_api(calls)))
    conn = connect(tmp_path / "index.db")
    for i, title in enumerate(ANSWERS, 1):
        conn.execute("INSERT INTO hackathons (id, source, source_id, url, title, organizer, submissions_count, "
                     "registrants, cash_usd_est, judges_count) VALUES (?, 'devpost', ?, ?, ?, 'Org', 80, 400, 0, 5)",
                     (i, str(i), f"https://e{i}.devpost.com/", title))
    conn.commit()
    return conn


def test_verdicts():
    assert le.normalise(ANSWERS["Logitech Build"])["verdict"] == "worth"    # hardware counts
    assert le.normalise(ANSWERS["Google Meetup"])["verdict"] == "worth"     # strong opportunity counts
    assert le.normalise(ANSWERS["Bogus Fund"])["verdict"] == "skip"         # cash, but not legit


def test_sample_writes_review_only(tmp_path, monkeypatch):
    calls = []
    conn = setup(tmp_path, monkeypatch, calls)
    out = le.sample(conn, tmp_path, 3, ["model-a", "model-b"], workers=2, max_cost=1)
    text = out.read_text()
    assert "Logitech Build" in text and "**model-b**: **worth**" in text
    assert len(calls) == 6
    assert conn.execute("SELECT COUNT(*) FROM event_labels").fetchone()[0] == 0


def test_full_run_resumes_and_feeds_scoring(tmp_path, monkeypatch):
    calls = []
    conn = setup(tmp_path, monkeypatch, calls)
    first = le.full_run(conn, "model-a", workers=2, max_cost=1, snapshot_path=None, limit=2)
    assert len(calls) == 2
    assert le.full_run(conn, "model-a", workers=2, max_cost=1, snapshot_path=None) == first   # resumed
    assert len(calls) == 3                                                                 # only the missing one
    assert conn.execute("SELECT finished_at FROM enrich_runs WHERE run_id=?", (first,)).fetchone()[0]
    assert le.full_run(conn, "model-a", workers=2, max_cost=1, snapshot_path=None) != first  # finished: new run

    score_events(conn)
    tiers = dict(conn.execute("SELECT h.title, s.tier FROM event_scores_latest s "
                              "JOIN hackathons h ON h.id = s.hackathon_id").fetchall())
    # Rules alone would call both no-cash events junk; the labels keep them.
    assert tiers == {"Logitech Build": "ok", "Google Meetup": "ok", "Bogus Fund": "junk"}


def test_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("# key\nexport OPENROUTER_API_KEY='sk-or-x'\n")
    llm.load_env(tmp_path / ".env")
    import os
    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-x"
