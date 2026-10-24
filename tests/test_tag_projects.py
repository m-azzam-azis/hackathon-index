"""Project tagging against a fake OpenAI-compatible API: only projects from A/B events are
tagged, the model never sees who won, and full runs are versioned and resumable."""

import json

import httpx

from hackathon_index import llm
from hackathon_index import tag_projects as tp
from hackathon_index.db import connect

ANSWER = {"domain": "health", "ai_role": "agent", "form": "web_app", "sponsor_tech": "central",
          "originality": 7, "technical_depth": 8, "polish": 6, "specific_user": True, "real_use": False,
          "summary": "Triage agent for clinics."}


def setup(tmp_path, monkeypatch, prompts):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")

    def handler(request):
        prompts.append(json.loads(request.content)["messages"][1]["content"])
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(ANSWER)}}],
                                         "usage": {"cost": 0.0005}})

    original = llm.Client.__init__
    monkeypatch.setattr(tp.Client, "__init__",
                        lambda self, model, **kw: original(self, model, transport=httpx.MockTransport(handler)))
    conn = connect(tmp_path / "index.db")
    conn.execute("INSERT INTO enrich_runs (run_id, step, created_at) VALUES ('events-1', 'events', '2026')")
    for hid, grade in ((1, "B"), (2, "C")):
        conn.execute("INSERT INTO hackathons (id, source, source_id, url, title) VALUES (?, 'devpost', ?, ?, ?)",
                     (hid, str(hid), f"https://e{hid}.devpost.com/", f"Event {hid}"))
        conn.execute("INSERT INTO event_labels (run_id, hackathon_id, grade) VALUES ('events-1', ?, ?)", (hid, grade))
    conn.execute("INSERT INTO prizes (hackathon_id, position, title) VALUES (1, 0, 'Best Use of Gemini')")
    for pid in range(1, 7):
        hid = 1 if pid <= 4 else 2
        conn.execute("INSERT INTO projects (id, source, source_id, url, title, built_with, writeup, detail_fetched) "
                     "VALUES (?, 'devpost', ?, ?, ?, '[\"gemini\"]', ?, 1)",
                     (pid, str(pid), f"https://devpost.com/software/p{pid}", f"Project {pid}",
                      json.dumps({"Inspiration": "Clinics are overloaded."})))
        conn.execute("INSERT INTO submissions (hackathon_id, project_id, is_winner) VALUES (?, ?, ?)",
                     (hid, pid, int(pid in (1, 5))))
    conn.commit()
    return conn


def test_only_good_events_and_blind_to_winning(tmp_path, monkeypatch):
    prompts = []
    conn = setup(tmp_path, monkeypatch, prompts)
    assert sorted(p["id"] for p, *_ in tp.targets(conn)) == [1, 2, 3, 4]
    assert len(tp.targets(conn, "ABC")) == 6
    out = tp.sample(conn, tmp_path, 2, ["model-a"], workers=2, max_cost=1)
    text = out.read_text()
    assert "(1 winners)" in text and "health · agent · web_app · sponsor central · 7 / 8 / 6" in text
    assert "$0.0010 | 0 of 2 | $0.00 for 4 projects in AB events" in text
    assert all("Best Use of Gemini" in p and "win" not in p.lower() for p in prompts)
    assert conn.execute("SELECT COUNT(*) FROM project_tags").fetchone()[0] == 0


def test_full_run_resumes(tmp_path, monkeypatch):
    prompts = []
    conn = setup(tmp_path, monkeypatch, prompts)
    first = tp.full_run(conn, "model-a", workers=2, max_cost=1, snapshot_path=None, limit=3)
    assert len(prompts) == 3
    assert tp.full_run(conn, "model-a", workers=2, max_cost=1, snapshot_path=None) == first
    assert len(prompts) == 4
    row = conn.execute("SELECT domain, ai_role, hackathon_id FROM project_tags_latest WHERE project_id=1").fetchone()
    assert tuple(row) == ("health", "agent", 1)
    assert conn.execute("SELECT finished_at FROM enrich_runs WHERE run_id=?", (first,)).fetchone()[0]


def test_normalise_rejects_unknown_values():
    tags = tp.normalise({"domain": "space", "ai_role": "magic", "originality": 14})
    assert (tags["domain"], tags["ai_role"], tags["originality"]) == ("other", None, 10)
