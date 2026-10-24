"""Project tagging against a fake OpenAI-compatible API: only projects from A/B events are
tagged, batches mix winners and non-winners from one event, the model never sees who won,
and full runs are versioned and resumable."""

import json
import re

import httpx

from hackathon_index import llm
from hackathon_index import tag_projects as tp
from hackathon_index.db import connect


def answer_for(prompt):
    """Tags every project in the batch; the fake 'judge' likes Project 1 and Project 5 best."""
    items = []
    for n, title in re.findall(r"### Project (\d+): (.+)", prompt):
        best = title in ("Project 1", "Project 5")
        items.append({"n": int(n), "title": title, "domain": "health", "ai_role": "agent", "form": "web_app",
                      "sponsor_tech": "central", "originality": 9 if best else 3, "technical_depth": 8 if best else 4,
                      "polish": 7 if best else 2, "rank": 1 if best else 2, "specific_user": True,
                      "real_use": False, "summary": f"{title} triages patients."})
    return {"projects": items}


def setup(tmp_path, monkeypatch, prompts, projects=12):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")

    def handler(request):
        prompt = json.loads(request.content)["messages"][1]["content"]
        prompts.append(prompt)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer_for(prompt))}}],
                                         "usage": {"cost": 0.002}})

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
    for pid in range(1, projects + 1):
        hid = 2 if pid == projects else 1      # the last project is in the C event
        conn.execute("INSERT INTO projects (id, source, source_id, url, title, built_with, writeup, detail_fetched) "
                     "VALUES (?, 'devpost', ?, ?, ?, '[\"gemini\"]', ?, 1)",
                     (pid, str(pid), f"https://devpost.com/software/p{pid}", f"Project {pid}",
                      json.dumps({"Inspiration": "Clinics are overloaded."})))
        conn.execute("INSERT INTO submissions (hackathon_id, project_id, is_winner) VALUES (?, ?, ?)",
                     (hid, pid, int(pid in (1, 5))))
    conn.commit()
    return conn


def test_batches_are_balanced_and_from_one_event(tmp_path, monkeypatch):
    conn = setup(tmp_path, monkeypatch, [], projects=21)
    groups = tp.batches(tp.targets(conn))
    assert sorted(len(rows) for _, rows in groups) == [6, 7, 7]          # 20 split evenly, not 8 + 8 + 4
    assert all(len({hid for _, hid, _ in rows}) == 1 for _, rows in groups)
    assert len(tp.targets(conn, "ABC")) == 21


def test_sample_reports_separation_and_is_blind(tmp_path, monkeypatch):
    prompts = []
    conn = setup(tmp_path, monkeypatch, prompts)
    out = tp.sample(conn, tmp_path, 11, ["model-a"], workers=2, max_cost=1)
    text = out.read_text()
    assert "11 projects in 2 batches (2 winners)" in text
    assert "| 100% / 100% (" in text                                   # the fake judge always prefers winners
    assert "rank 1 of " in text and "health · agent · web_app · sponsor central · 9 / 8 / 7" in text
    assert all("Best Use of Gemini" in p and "win" not in p.lower() for p in prompts)
    assert conn.execute("SELECT COUNT(*) FROM project_tags").fetchone()[0] == 0


def test_full_run_resumes(tmp_path, monkeypatch):
    prompts = []
    conn = setup(tmp_path, monkeypatch, prompts)
    first = tp.full_run(conn, "model-a", workers=2, max_cost=1, snapshot_path=None, limit=1)
    assert len(prompts) == 1
    assert tp.full_run(conn, "model-a", workers=2, max_cost=1, snapshot_path=None) == first
    assert len(prompts) == 2
    assert conn.execute("SELECT COUNT(*) FROM project_tags_latest").fetchone()[0] == 11
    row = conn.execute("SELECT domain, batch_rank, batch_size FROM project_tags_latest WHERE project_id=1").fetchone()
    assert tuple(row) == ("health", 1, 5) or tuple(row) == ("health", 1, 6)
    assert conn.execute("SELECT finished_at FROM enrich_runs WHERE run_id=?", (first,)).fetchone()[0]


class Fake:
    """Replies with each answer in turn, counting calls."""
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    def json(self, system, user, max_tokens):
        self.calls += 1
        return self.answers[min(self.calls, len(self.answers)) - 1], {"cost": 0.001}


BATCH = [(10, "GeoTaste", "GeoTaste maps business opportunities by neighbourhood with Qloo and Mapbox"),
         (11, "MedPrompt AI", "MedPrompt AI checks symptoms and finds first aid help fast"),
         (12, "Zesty", "Zesty flips your feed into an unrecommendation engine of opposite tastes")]


def reply(*rows):
    return {"projects": [{"n": n, "title": t, "summary": s, "domain": "other"} for n, t, s in rows]}


def test_incomplete_reply_is_an_error():
    fake = Fake({"projects": [{"n": 1, "title": "GeoTaste", "domain": "health"}]})
    tags, cost, error = tp._tag(fake, ("prompt", BATCH))
    assert tags is None and "1 of 3" in error and fake.calls == tp.ATTEMPTS


def test_misnumbered_answers_are_read_by_title():
    misnumbered = reply((1, "MedPrompt AI", "Checks symptoms and finds first aid help."),
                        (2, "Zesty", "An unrecommendation engine of opposite tastes."),
                        (3, "GeoTaste", "Maps business opportunities by neighbourhood with Qloo."))
    tags, _, error = tp._tag(Fake(misnumbered), ("prompt", BATCH))
    assert error is None and "Qloo" in tags[10]["summary"] and "first aid" in tags[11]["summary"]


def test_answer_on_the_wrong_project_is_retried_then_kept():
    shifted = reply((1, "GeoTaste", "Checks symptoms and finds first aid help fast."),
                    (2, "MedPrompt AI", "An unrecommendation engine of opposite tastes."),
                    (3, "Zesty", "Maps business opportunities by neighbourhood with Qloo and Mapbox."))
    right = reply((1, "GeoTaste", "Maps business opportunities by neighbourhood with Qloo."),
                  (2, "MedPrompt AI", "Checks symptoms and finds first aid help."),
                  (3, "Zesty", "An unrecommendation engine of opposite tastes."))
    fake = Fake(shifted, right)
    tags, cost, error = tp._tag(fake, ("prompt", BATCH))
    assert error is None and fake.calls == 2 and round(cost, 3) == 0.002
    assert "Qloo" in tags[10]["summary"]


def test_summary_that_reads_like_a_neighbour_is_rejected():
    # Titles echoed correctly, but GeoTaste's answer describes the first aid app.
    swapped = reply((1, "GeoTaste", "Checks symptoms and finds first aid help fast."),
                    (2, "MedPrompt AI", "Maps business opportunities by neighbourhood with Qloo and Mapbox."),
                    (3, "Zesty", "An unrecommendation engine of opposite tastes."))
    tags, _, error = tp._tag(Fake(swapped), ("prompt", BATCH))
    assert tags is None and "GeoTaste" in error


def test_title_matching():
    assert tp.same_title("Riḥla", "Rihla") and tp.same_title("GeoTaste: Your Business Consultant Agent", "GeoTaste")
    assert tp.same_title("A+ Foundary", "A+ Foundry")
    assert not tp.same_title("GeoTaste", "MedPrompt AI") and tp.same_title(".", "anything")


def test_audit_finds_misplaced_summaries(tmp_path, monkeypatch):
    conn = setup(tmp_path, monkeypatch, [], projects=4)
    conn.execute("INSERT INTO enrich_runs (run_id, step, created_at) VALUES ('tags-1', 'tags', '2026')")
    texts = {1: "solar flare forecasting from NASA satellite images", 2: "soil moisture sensors skip irrigation",
             3: "voice tutor for blind students reading notes aloud"}
    for pid, text in texts.items():
        conn.execute("UPDATE projects SET writeup=? WHERE id=?", (json.dumps({"About": text}), pid))
    summaries = {1: "Forecasts solar flares from satellite images.", 2: "Reads notes aloud for blind students.",
                 3: "Soil moisture sensors decide when to skip irrigation."}
    for pid, summary in summaries.items():
        conn.execute("INSERT INTO project_tags (run_id, project_id, hackathon_id, summary) VALUES ('tags-1', ?, 1, ?)",
                     (pid, summary))
    total, suspect, ids = tp.audit(conn)
    assert (total, suspect, sorted(ids)) == (3, 2, [2, 3])


def test_normalise_rejects_unknown_values():
    tags = tp.normalise({"domain": "space", "ai_role": "magic", "originality": 14, "rank": 9}, size=4)
    assert (tags["domain"], tags["ai_role"], tags["originality"], tags["batch_rank"]) == ("other", None, 10, None)
