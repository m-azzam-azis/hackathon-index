"""Event labeling against a fake OpenAI-compatible API: the sample mode leaves the database
alone, full runs are versioned and resumable, and scoring v2 trusts the labels."""

import json

import httpx

from hackathon_index import label_events as le
from hackathon_index import llm
from hackathon_index.db import connect
from hackathon_index.score import score_events

ANSWERS = {  # by event title
    "Logitech Build": {"products_usd": 6000, "event_type": "company_challenge", "winner_quality": 7,
                       "stakes": 7, "sponsor_caliber": 9, "competition": 7, "legit": 90},
    "Google Meetup": {"opportunities": ["meet the Google Cloud team"], "event_type": "professional_open",
                      "winner_quality": 8, "stakes": 6, "sponsor_caliber": 10, "competition": 8, "legit": 85},
    "Campus Weekend": {"products_usd": 400, "event_type": "student_local", "stock_prizes": True,
                       "winner_quality": 3, "stakes": 3, "sponsor_caliber": 3, "competition": 3, "legit": 90},
    "Bogus Fund": {"cash_usd": 10000, "winner_quality": 6, "stakes": 9, "sponsor_caliber": 1,
                   "competition": 2, "legit": 20, "inflated": True},
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


def test_grades():
    grades = {t: le.normalise(a)["grade"] for t, a in ANSWERS.items()}
    # Hardware and opportunities count; a legit but stock-prize campus weekend and an
    # unverifiable big-money event both grade low.
    assert grades == {"Logitech Build": "B", "Google Meetup": "A", "Campus Weekend": "D", "Bogus Fund": "D"}


def test_sample_writes_review_only(tmp_path, monkeypatch):
    calls = []
    conn = setup(tmp_path, monkeypatch, calls)
    anchors = tmp_path / "anchors.txt"
    anchors.write_text("+ Google Meetup\n- https://e3.devpost.com/\n~ Not In Database\n")
    out = le.sample(conn, tmp_path, 2, ["model-a", "model-b"], workers=2, max_cost=1, anchors_path=anchors)
    text = out.read_text()
    assert "**model-b**: **A**" in text and "your anchor: great" in text
    assert "| model-a | $0.0030 | 0 of 3 | 2 of 2 |" in text     # 2 anchors + sampled events, no repeats
    assert len(calls) == 6
    assert conn.execute("SELECT COUNT(*) FROM event_labels").fetchone()[0] == 0


def test_full_run_resumes_and_feeds_scoring(tmp_path, monkeypatch):
    calls = []
    conn = setup(tmp_path, monkeypatch, calls)
    first = le.full_run(conn, "model-a", workers=2, max_cost=1, snapshot_path=None, limit=2)
    assert len(calls) == 2
    assert le.full_run(conn, "model-a", workers=2, max_cost=1, snapshot_path=None) == first   # resumed
    assert len(calls) == 4                                                                 # only the missing ones
    assert conn.execute("SELECT finished_at FROM enrich_runs WHERE run_id=?", (first,)).fetchone()[0]
    assert le.full_run(conn, "model-a", workers=2, max_cost=1, snapshot_path=None) != first  # finished: new run

    score_events(conn)
    tiers = dict(conn.execute("SELECT h.title, s.tier FROM event_scores_latest s "
                              "JOIN hackathons h ON h.id = s.hackathon_id").fetchall())
    # Rules alone would call both no-cash events junk; the grades keep them and drop the rest.
    assert tiers == {"Logitech Build": "ok", "Google Meetup": "ok", "Campus Weekend": "junk", "Bogus Fund": "junk"}


def test_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("# key\nexport OPENROUTER_API_KEY='sk-or-x'\n")
    llm.load_env(tmp_path / ".env")
    import os
    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-x"


def test_cut_off_reply_is_retried_with_more_tokens(monkeypatch):
    budgets = []

    def handler(request):
        body = json.loads(request.content)
        budgets.append(body["max_tokens"])
        if len(budgets) == 1:   # reasoning ate the budget: empty answer, finish_reason=length
            return httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"legit": 80}'}}], "usage": {}})

    client = llm.Client("m", api_key="k", transport=httpx.MockTransport(handler))
    assert client.json("s", "u")[0] == {"legit": 80}
    assert budgets == [4000, 8000]
