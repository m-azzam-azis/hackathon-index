"""LLM tags per project: what it is, how it uses AI, and how strong it looks on paper.

One call per project reads the title, tagline, built-with list, writeup and the event's prize
tracks, and returns fixed-list tags (domain, AI role, form, sponsor-tech use), three 0-10
scores and a one-line summary. The model is never told whether the project won, so winners
and the sampled non-winners are tagged the same way and win rates can be compared fairly.
Only projects from events scored `ok` are tagged.

    tag-projects --sample 40 --compare a,b    test: review file only, the database is untouched
    tag-projects --model M                    full run: snapshot first, then a new versioned run
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .db import new_run_id
from .label_events import _num, _run_all, _score10
from .llm import Client, LLMError
from .progress import Progress

PROMPT_VERSION = "1"
DEFAULT_MODEL = "anthropic/claude-haiku-5.5"
COMPARE_MODELS = ["anthropic/claude-haiku-5.5", "deepseek/deepseek-v4.1-flash"]

DOMAINS = ("health", "education", "fintech", "climate", "accessibility", "devtools", "productivity",
           "social", "gaming", "security", "commerce", "civic", "science", "other")
AI_ROLES = ("none", "llm_wrapper", "rag", "agent", "multi_agent", "custom_ml", "computer_vision", "voice")
FORMS = ("web_app", "mobile_app", "browser_extension", "hardware", "bot", "game", "ar_vr",
         "api_library", "data_analysis", "other")
SPONSOR_TECH = ("central", "passing", "none")

SYSTEM = f"""You tag hackathon projects for a study of what wins. Read the project and answer with
one JSON object and nothing else:
{{
  "domain": one of {", ".join(DOMAINS)},
  "ai_role": the most advanced way AI is actually used, one of {", ".join(AI_ROLES)}
      (llm_wrapper = prompts an LLM or a chatbot; rag = retrieval over the project's own data;
      agent = an LLM that plans and calls tools over several steps; custom_ml = a model the
      team trained or fine-tuned),
  "form": what was built, one of {", ".join(FORMS)},
  "sponsor_tech": how the event's sponsor tools are used, one of {", ".join(SPONSOR_TECH)}
      (central = the project is built around them; passing = used for one part),
  "originality": 0-10, how fresh the idea is (3 = a common hackathon idea such as a study
      planner or a recycling scanner; 8 = rarely seen),
  "technical_depth": 0-10, how hard it was to build (3 = one API call behind a UI; 8 = custom
      models, hardware, or several systems working together),
  "polish": 0-10, how complete and convincing it looks from the writeup and links,
  "specific_user": true if it names a specific user or customer and their problem,
  "real_use": true if it shows real users, a pilot, or a live deployment,
  "summary": one plain sentence on what it does
}}
Judge what the writeup shows, not its length or buzzwords."""


def project_prompt(conn, p, hackathon_id: int, writeup_chars: int = 3000) -> str:
    h = conn.execute("SELECT title FROM hackathons WHERE id=?", (hackathon_id,)).fetchone()
    tracks = [r[0] for r in conn.execute("SELECT title FROM prizes WHERE hackathon_id=? ORDER BY position "
                                         "LIMIT 15", (hackathon_id,))]
    text = " ".join(" ".join(f"{k}: {v}" for k, v in json.loads(p["writeup"] or "{}").items()).split())
    return "\n".join([
        f"Title: {p['title']}",
        f"Tagline: {p['tagline'] or '-'}",
        f"Event: {h['title'] if h else '?'} · prize tracks: {'; '.join(tracks) or '-'}",
        f"Built with: {', '.join(json.loads(p['built_with'] or '[]')[:20]) or '-'}",
        f"Repo: {'yes' if p['has_repo'] else 'no'} · video: {'yes' if p['has_video'] else 'no'} · "
        f"team of {p['team_size'] or '?'}",
        "", text[:writeup_chars],
    ])


def _pick(value, allowed, fallback=None):
    return value if value in allowed else fallback


def normalise(a: dict) -> dict:
    return {
        "domain": _pick(a.get("domain"), DOMAINS, "other"),
        "ai_role": _pick(a.get("ai_role"), AI_ROLES),
        "form": _pick(a.get("form"), FORMS, "other"),
        "sponsor_tech": _pick(a.get("sponsor_tech"), SPONSOR_TECH),
        "originality": _score10(a.get("originality")),
        "technical_depth": _score10(a.get("technical_depth")),
        "polish": _score10(a.get("polish")),
        "specific_user": int(bool(a.get("specific_user"))),
        "real_use": int(bool(a.get("real_use"))),
        "summary": str(a.get("summary") or "")[:300],
    }


TAG_COLUMNS = ("domain", "ai_role", "form", "sponsor_tech", "originality", "technical_depth", "polish",
               "specific_user", "real_use", "summary")


def _tag(client: Client, prompt: str) -> tuple[dict | None, float, str | None]:
    try:
        answer, usage = client.json(SYSTEM, prompt)
        return normalise(answer), _num(usage.get("cost")), None
    except (LLMError, ValueError) as exc:
        return None, 0.0, str(exc)[:300]


def targets(conn) -> list[tuple]:
    """(project row, hackathon_id, is_winner) for every full project page in an `ok` event,
    or in any event before the first scoring run. A project entered in several events is
    tagged once, against the first of them."""
    scored = conn.execute("SELECT 1 FROM event_scores_latest LIMIT 1").fetchone()
    ok = "AND s.hackathon_id IN (SELECT hackathon_id FROM event_scores_latest WHERE tier='ok')" if scored else ""
    rows = conn.execute(
        f"SELECT p.*, MIN(s.hackathon_id) AS hid, MAX(s.is_winner) AS won FROM projects p "
        f"JOIN submissions s ON s.project_id = p.id WHERE p.detail_fetched = 1 {ok} "
        f"GROUP BY p.id ORDER BY p.id").fetchall()
    return [(r, r["hid"], r["won"] or 0) for r in rows]


def pick_sample(conn, n: int) -> list[tuple]:
    """Deterministic: half winners, half non-winners."""
    rows = sorted(targets(conn), key=lambda t: hashlib.sha1(t[0]["url"].encode()).hexdigest())
    won = [t for t in rows if t[2]][:n - n // 2]
    lost = [t for t in rows if not t[2]][:n // 2]
    return won + lost


def sample(conn, data: Path, n: int, models: list[str], workers: int, max_cost: float) -> Path:
    """Tag a sample with each model and write a side-by-side review file. No DB writes."""
    picked = pick_sample(conn, n)
    prompts = [(p["id"], project_prompt(conn, p, hid)) for p, hid, _ in picked]
    results, summary = {}, {}
    for model in models:
        prog = Progress(f"sample {model}", len(prompts))
        rows = list(_run_all(Client(model), prompts, workers, prog, max_cost, label=_tag))
        results[model] = {k: (lab, err) for k, lab, _, err in rows}
        summary[model] = (sum(c for _, _, c, _ in rows), sum(1 for *_, e in rows if e))
        prog.done()

    out = data / "review" / f"projects-sample-{datetime.now():%Y%m%d-%H%M%S}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Project tags: {len(picked)} projects ({sum(w for *_, w in picked)} winners)", "",
             "Per model: domain · AI role · form · sponsor tech · originality / depth / polish (0-10) · "
             "specific user · real use · summary. The models aren't told who won.", "",
             "| Model | Cost | Errors | Projected full run |", "|---|---|---|---|"]
    total = len(targets(conn))
    lines += [f"| {m} | ${c:.4f} | {e} of {len(picked)} | ${c / max(len(picked), 1) * total:.2f} "
              f"for {total:,} projects |" for m, (c, e) in summary.items()]
    lines.append("")
    for p, hid, won in picked:
        event = conn.execute("SELECT title FROM hackathons WHERE id=?", (hid,)).fetchone()[0]
        lines += [f"## {p['title']}{' (winner)' if won else ''}", f"{p['url']} · {event}", ""]
        for model in models:
            lab, err = results[model].get(p["id"], (None, "not run"))
            if lab is None:
                lines.append(f"- **{model}**: error: {err}")
                continue
            lines.append(
                f"- **{model}**: {lab['domain']} · {lab['ai_role']} · {lab['form']} · sponsor {lab['sponsor_tech']} · "
                f"{lab['originality']} / {lab['technical_depth']} / {lab['polish']} · "
                f"{'user' if lab['specific_user'] else 'no user'} · {'real use' if lab['real_use'] else 'no use'} · "
                f"{lab['summary']}")
        lines.append("")
    out.write_text("\n".join(lines))
    print(f"\nreview file: {out}")
    return out


def full_run(conn, model: str, workers: int, max_cost: float, snapshot_path: str | None,
             limit: int | None = None) -> str:
    """Tag every target project. Resumes the newest unfinished run with the same model and
    prompt; otherwise starts a new run (earlier runs are kept)."""
    params = json.dumps({"model": model, "prompt": PROMPT_VERSION})
    row = conn.execute("SELECT run_id FROM enrich_runs WHERE step='tags' AND params=? "
                       "AND finished_at IS NULL ORDER BY created_at DESC LIMIT 1", (params,)).fetchone()
    if row:
        run_id = row[0]
        print(f"resuming {run_id}")
    else:
        now = datetime.now(timezone.utc)
        run_id = new_run_id(conn, "tags", now)
        conn.execute("INSERT INTO enrich_runs (run_id, step, rules, params, snapshot, created_at) "
                     "VALUES (?, 'tags', ?, ?, ?, ?)", (run_id, PROMPT_VERSION, params, snapshot_path, now.isoformat()))
        conn.commit()
    done = {r[0] for r in conn.execute("SELECT project_id FROM project_tags WHERE run_id=? AND error IS NULL", (run_id,))}
    every = targets(conn)
    todo = [t for t in every if t[0]["id"] not in done][:limit]
    prog = Progress("tag projects", len(todo))
    errors = 0
    jobs = [((p["id"], hid), project_prompt(conn, p, hid)) for p, hid, _ in todo]
    for (pid, hid), lab, cost, error in _run_all(Client(model), jobs, workers, prog, max_cost, label=_tag):
        lab = lab or {}
        errors += bool(error)
        row = {"run_id": run_id, "project_id": pid, "hackathon_id": hid, "cost": cost, "error": error,
               **{k: lab.get(k) for k in TAG_COLUMNS}}
        conn.execute(f"INSERT OR REPLACE INTO project_tags ({', '.join(row)}) "
                     f"VALUES ({', '.join('?' * len(row))})", list(row.values()))
        conn.commit()
    prog.done()
    done = {r[0] for r in conn.execute("SELECT project_id FROM project_tags WHERE run_id=? AND error IS NULL", (run_id,))}
    left = sum(1 for p, *_ in every if p["id"] not in done)
    if left == 0:
        conn.execute("UPDATE enrich_runs SET finished_at=? WHERE run_id=?",
                     (datetime.now(timezone.utc).isoformat(), run_id))
        conn.commit()
    cost = conn.execute("SELECT SUM(cost) FROM project_tags WHERE run_id=?", (run_id,)).fetchone()[0] or 0
    print(f"{run_id}: {len(done):,} projects tagged · ${cost:.2f} · {errors} errors"
          + (f" · {left:,} left, re-run to finish" if left else ""))
    return run_id
