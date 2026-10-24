"""explore.db and the MCP tools, on a tiny graded and tagged database."""

import json
import sqlite3

from hackathon_index import explore, mcp_server
from hackathon_index.db import connect


def make(tmp_path):
    conn = connect(tmp_path / "index.db")
    conn.execute("INSERT INTO enrich_runs (run_id, step, created_at) VALUES ('events-1', 'events', '1')")
    conn.execute("INSERT INTO enrich_runs (run_id, step, created_at) VALUES ('tags-1', 'tags', '2')")
    for hid, grade in ((1, "A"), (2, "B")):
        conn.execute("INSERT INTO hackathons (id, source, source_id, url, title, end_date, submissions_count, themes) "
                     "VALUES (?, 'devpost', ?, ?, ?, '2025-05-01', 50, '[\"AI\"]')",
                     (hid, str(hid), f"https://e{hid}.devpost.com/", f"Event {hid}"))
        conn.execute("INSERT INTO event_labels (run_id, hackathon_id, grade, quality) VALUES ('events-1', ?, ?, 80)",
                     (hid, grade))
    conn.execute("INSERT INTO people (id, source, profile_url, username) VALUES (1, 'devpost', 'https://devpost.com/ana', 'ana')")
    # In both events the winner builds around the sponsor's tech and the non-winners don't.
    pid = 0
    for hid in (1, 2):
        for won in (1, 0, 0):
            pid += 1
            conn.execute("INSERT INTO projects (id, source, source_id, url, title, tagline, built_with, links, "
                         "video_urls, has_repo, has_video, team_size, writeup_chars, detail_fetched) "
                         "VALUES (?, 'devpost', ?, ?, ?, 'Voice agent for clinics', ?, ?, ?, 1, 1, 2, 3000, 1)",
                         (pid, str(pid), f"https://devpost.com/software/p{pid}", f"Project {pid}",
                          json.dumps(["gemini", "python"] if won else ["python"]),
                          json.dumps(["https://github.com/x/p"]), json.dumps(["https://youtu.be/abc"])))
            conn.execute("INSERT INTO submissions (hackathon_id, project_id, is_winner) VALUES (?, ?, ?)", (hid, pid, won))
            conn.execute("INSERT INTO project_tags (run_id, project_id, hackathon_id, domain, ai_role, form, "
                         "sponsor_tech, originality, technical_depth, polish, batch_rank, batch_size, specific_user, "
                         "real_use, summary) VALUES ('tags-1', ?, ?, 'health', ?, 'web_app', ?, ?, 6, 6, ?, 3, 1, 0, ?)",
                         (pid, hid, "agent" if won else "llm_wrapper", "central" if won else "none",
                          8 if won else 4, 1 if won else 2, f"Summary {pid} about elderly voice care"))
            if won:
                conn.execute("INSERT INTO submission_prizes VALUES (?, ?, 'Grand Prize')", (hid, pid))
                conn.execute("INSERT INTO project_members VALUES (?, 1)", (pid,))
    conn.commit()
    return conn


def test_build(tmp_path):
    make(tmp_path)
    db = explore.build(tmp_path)
    out = sqlite3.connect(db)
    p = out.execute("SELECT won, prizes, repo_url, video_url, combined, grade, members FROM projects "
                    "WHERE project_id = 1").fetchone()
    assert p == (1, "Grand Prize", "https://github.com/x/p", "https://youtu.be/abc", 20, "A", "https://devpost.com/ana")
    gap = out.execute("SELECT within_event_gap, events FROM traits WHERE scope = 'A and B' "
                      "AND trait = 'sponsor_tech' AND value = 'central'").fetchone()
    assert gap == (100.0, 2)
    assert out.execute("SELECT wins_ab, wins_all FROM builders WHERE username = 'ana'").fetchone() == (2, 2)
    assert out.execute("SELECT tagged_projects, tagged_winners FROM events WHERE id = 1").fetchone() == (3, 1)
    assert not explore.stale(tmp_path)


def test_mcp_tools_are_read_only(tmp_path):
    make(tmp_path)
    explore.build(tmp_path)
    conn = mcp_server._connect(tmp_path / "explore.db")
    assert "| title |" in mcp_server.run_sql(conn, "SELECT title FROM projects WHERE won = 1")
    assert "SQL error" in mcp_server.run_sql(conn, "DELETE FROM projects")
    assert mcp_server.run_sql(conn, "SELECT COUNT(*) AS n FROM projects").endswith("| 6 |")
    app = mcp_server.server(tmp_path)
    assert app.name == "hackathon-index"
    assert "hackathon_index --data" in mcp_server.config(tmp_path, "/venv/bin/python")
