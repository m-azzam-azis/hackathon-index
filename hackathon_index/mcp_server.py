"""MCP server: lets Claude (Claude Code or Claude Desktop, on a normal subscription) query the
index read-only. `hackathon-index mcp` speaks MCP over stdio; `hackathon-index mcp-config`
prints the lines that register it.

Tools: describe, sql, what_wins, search_projects. Everything is read-only: explore.db and
index.db are opened with mode=ro, and long queries are cut off.
"""

import sqlite3
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import explore

MAX_ROWS = 200
MAX_SECONDS = 10

INSTRUCTIONS = """Past hackathons from Devpost (2022 onward, events with at least $1k cash), their
winning projects and a capped sample of non-winners, graded and tagged by an LLM.
Start with `describe`. Non-winners are sampled, so never read raw win rates as a project's
chance of winning; compare winners with non-winners from the same event (`what_wins` does).
Event grades: A/B = worth learning from, C = maybe, D = skip. Project scores (originality,
technical_depth, polish, 0-10) are relative to projects from the same event."""

DESCRIPTION = """explore.db (default for `sql`):
  events(id, title, url, organizer, year, end_date, online, registrants, submissions, grade A-D,
         quality 0-100, event_type, cash_usd, products_usd, credits_usd, stakes, sponsor_caliber,
         competition, tier ok/watch/junk, grade_reason, themes, tagged_projects, tagged_winners)
  projects(project_id, title, url, tagline, summary, won 0/1, prizes, event, event_url, event_id,
           grade, year, domain, ai_role, form, sponsor_tech central/passing/none, originality,
           technical_depth, polish, combined = sum of the three, batch_rank (1 = best of the
           projects scored together), batch_size, specific_user, real_use, has_repo, repo_url,
           has_video, video_url, team_size, likes, writeup_chars, built_with (comma list),
           members (Devpost profile URLs, one per line))
      Only projects from events graded A or B.
  builders(profile_url, username, wins_ab, wins_all, projects_fetched, events, winning_projects)
  traits(scope 'A and B'/'A'/'B', trait, value, projects, winner_rate %, nonwinner_rate %,
         within_event_gap (percentage points), events)
  projects_fts: full-text index; JOIN projects ON projects.project_id = projects_fts.rowid
      WHERE projects_fts MATCH 'agent memory'

index.db (sql with raw=true): the full crawl. Useful tables: hackathons, prizes, judges,
  projects (writeup is JSON {section: text}, links, built_with), submissions(hackathon_id,
  project_id, is_winner, gallery_rank), submission_prizes, submission_categories, people,
  project_members, event_labels_latest, project_tags_latest, event_scores_latest."""


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, check_same_thread=False)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _table(cursor, rows) -> str:
    cols = [d[0] for d in cursor.description]
    cell = lambda v: "" if v is None else str(v).replace("\n", " ").replace("|", "/")[:300]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows]
    return "\n".join(lines)


def run_sql(conn: sqlite3.Connection, query: str, params=()) -> str:
    deadline = time.monotonic() + MAX_SECONDS
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
    try:
        cur = conn.execute(query, params)
        if cur.description is None:
            return "No rows (this connection is read-only)."
        rows = cur.fetchmany(MAX_ROWS + 1)
    except sqlite3.Error as exc:
        return f"SQL error: {exc}"
    finally:
        conn.set_progress_handler(None, 0)
    more = len(rows) > MAX_ROWS
    return _table(cur, rows[:MAX_ROWS]) + (f"\n(first {MAX_ROWS} rows; add LIMIT/OFFSET for more)" if more else "")


def server(data: Path) -> MCPServer:
    data = data.resolve()
    explore_db = explore.ensure(data)
    conns = {False: _connect(explore_db), True: _connect(data / "index.db")}
    app = MCPServer("hackathon-index", instructions=INSTRUCTIONS)

    @app.tool()
    def describe() -> str:
        """Tables, columns and caveats. Read this before writing SQL."""
        return DESCRIPTION

    @app.tool()
    def sql(query: str, raw: bool = False) -> str:
        """Run one read-only SQLite query and get a markdown table (at most 200 rows).
        raw=false queries explore.db (flat analysis tables); raw=true queries index.db (the full
        crawl, including writeups and prize text)."""
        return run_sql(conns[raw], query)

    @app.tool()
    def what_wins(scope: str = "A and B", trait: str = "", min_projects: int = 50) -> str:
        """Traits ranked by how much more often winners have them than non-winners from the same
        event (percentage points). scope: 'A and B', 'A' or 'B'. trait filters to one of domain,
        ai_role, form, sponsor_tech, flag, built_with."""
        q = ("SELECT trait, value, within_event_gap, winner_rate, nonwinner_rate, projects, events "
             "FROM traits WHERE scope = ? AND projects >= ? AND (? = '' OR trait = ?) "
             "ORDER BY within_event_gap DESC")
        return run_sql(conns[False], q, (scope, min_projects, trait, trait))

    @app.tool()
    def search_projects(text: str, winners_only: bool = False, limit: int = 20) -> str:
        """Full-text search over project titles, taglines, summaries and technologies, best
        matches first. Example: text='voice agent elderly'."""
        q = ("SELECT p.title, p.url, p.won, p.prizes, p.event, p.grade, p.summary, p.combined, "
             "p.video_url FROM projects_fts f JOIN projects p ON p.project_id = f.rowid "
             "WHERE projects_fts MATCH ? AND (? = 0 OR p.won = 1) ORDER BY rank LIMIT ?")
        terms = " OR ".join(f'"{w}"' for w in text.replace('"', " ").split()) or '""'
        return run_sql(conns[False], q, (terms, int(winners_only), min(limit, MAX_ROWS)))

    return app


def config(data: Path, python: str) -> str:
    data = str(data.resolve())
    args = ["-m", "hackathon_index", "--data", data, "mcp"]
    import json
    desktop = json.dumps({"mcpServers": {"hackathon-index": {"command": python, "args": args}}}, indent=2)
    return f"""Claude Code (run once, from any folder):
  claude mcp add --scope user hackathon-index -- {python} {' '.join(args)}

Claude Desktop: add this to ~/Library/Application Support/Claude/claude_desktop_config.json
(merge into "mcpServers" if the file already has one), then quit and reopen Claude Desktop:
{desktop}
"""
