"""LLM tags per project: what it is, how it uses AI, and how strong it looks on paper.

Projects are read in small batches from the same event, shuffled, so the model scores them
side by side: one project scored alone lands on a 5-7 every time, while a comparison spreads
the scores and adjusts for how strong the event was. Each project gets fixed-list tags
(domain, AI role, form, sponsor-tech use), three 0-10 scores, its rank within the batch and a
one-line summary. The model is never told who won, so winners and the sampled non-winners
are tagged the same way and win rates can be compared fairly. Only projects from events
graded A or B are tagged by default (`--grades ABC` adds C).

    tag-projects --sample 40 --compare a,b    test: review file only, the database is untouched
    tag-projects --model M [--grades AB]      full run: snapshot first, then a new versioned run
"""

import hashlib
import json
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

from .db import new_run_id
from .label_events import _num, _run_all, _score10
from .llm import Client, LLMError
from .progress import Progress

PROMPT_VERSION = "2"  # 1: one project per call; 2: batches from the same event, scored side by side
DEFAULT_MODEL = "anthropic/claude-haiku-5.5"
COMPARE_MODELS = ["anthropic/claude-haiku-5.5", "anthropic/claude-sonnet-5.5"]
BATCH = 8

DOMAINS = ("health", "education", "fintech", "climate", "accessibility", "devtools", "productivity",
           "social", "gaming", "security", "commerce", "civic", "science", "other")
AI_ROLES = ("none", "llm_wrapper", "rag", "agent", "multi_agent", "custom_ml", "computer_vision", "voice")
FORMS = ("web_app", "mobile_app", "browser_extension", "hardware", "bot", "game", "ar_vr",
         "api_library", "data_analysis", "other")
SPONSOR_TECH = ("central", "passing", "none")

SYSTEM = f"""You tag hackathon projects for a study of what wins. You get several projects
submitted to the same event. For each one, answer with one JSON object and nothing else:
{{"projects": [{{"n": the project's number,
  "domain": one of {", ".join(DOMAINS)},
  "ai_role": the most advanced way AI is actually used, one of {", ".join(AI_ROLES)}
      (llm_wrapper = prompts an LLM or a chatbot; rag = retrieval over the project's own data;
      agent = an LLM that plans and calls tools over several steps; custom_ml = a model the
      team trained or fine-tuned),
  "form": what was built, one of {", ".join(FORMS)},
  "sponsor_tech": how the event's sponsor tools are used, one of {", ".join(SPONSOR_TECH)}
      (central = the project is built around them; passing = used for one part),
  "originality": 0-10, how fresh the idea is,
  "technical_depth": 0-10, how hard it was to build,
  "polish": 0-10, how complete and convincing it looks from the writeup and links,
  "rank": 1 for the strongest entry overall in this batch, 2 for the next, and so on,
  "specific_user": true if it names a specific user or customer and their problem,
  "real_use": true if it shows real users, a pilot, or a live deployment,
  "summary": one plain sentence on what it does}}, ...]}}

Score the projects against each other and use the whole range: in a batch of several, the
strongest should usually get 8-10 and the weakest 1-4 on each score, and no two projects
should get identical scores unless they really are equal. As a fixed reference, 3 means a
common idea such as a study planner or one API call behind a UI, and 8 means rarely seen or
several systems working together. Judge what each writeup shows, not its length or
buzzwords."""


def _project_block(n: int, p, writeup_chars: int) -> str:
    text = " ".join(" ".join(f"{k}: {v}" for k, v in json.loads(p["writeup"] or "{}").items()).split())
    return "\n".join([
        f"### Project {n}: {p['title']}",
        f"Tagline: {p['tagline'] or '-'}",
        f"Built with: {', '.join(json.loads(p['built_with'] or '[]')[:20]) or '-'}",
        f"Repo: {'yes' if p['has_repo'] else 'no'} · video: {'yes' if p['has_video'] else 'no'} · "
        f"team of {p['team_size'] or '?'}",
        text[:writeup_chars],
    ])


def batch_prompt(conn, hackathon_id: int, projects: list, writeup_chars: int = 2000) -> str:
    h = conn.execute("SELECT title FROM hackathons WHERE id=?", (hackathon_id,)).fetchone()
    tracks = [r[0] for r in conn.execute("SELECT title FROM prizes WHERE hackathon_id=? ORDER BY position "
                                         "LIMIT 15", (hackathon_id,))]
    head = [f"Event: {h['title'] if h else '?'}", f"Prize tracks: {'; '.join(tracks) or '-'}",
            f"{len(projects)} projects:", ""]
    return "\n".join(head) + "\n\n".join(_project_block(i, p, writeup_chars) for i, p in enumerate(projects, 1))


def _pick(value, allowed, fallback=None):
    return value if value in allowed else fallback


def normalise(a: dict, size: int = 1) -> dict:
    rank = int(_num(a.get("rank"))) or None
    return {
        "domain": _pick(a.get("domain"), DOMAINS, "other"),
        "ai_role": _pick(a.get("ai_role"), AI_ROLES),
        "form": _pick(a.get("form"), FORMS, "other"),
        "sponsor_tech": _pick(a.get("sponsor_tech"), SPONSOR_TECH),
        "originality": _score10(a.get("originality")),
        "technical_depth": _score10(a.get("technical_depth")),
        "polish": _score10(a.get("polish")),
        "batch_rank": rank if rank and rank <= size else None,
        "batch_size": size,
        "specific_user": int(bool(a.get("specific_user"))),
        "real_use": int(bool(a.get("real_use"))),
        "summary": str(a.get("summary") or "")[:300],
    }


TAG_COLUMNS = ("domain", "ai_role", "form", "sponsor_tech", "originality", "technical_depth", "polish",
               "batch_rank", "batch_size", "specific_user", "real_use", "summary")


def _tag(client: Client, prompt_and_ids: tuple) -> tuple[dict | None, float, str | None]:
    """One batch. Returns ({project_id: tags}, cost, error)."""
    prompt, ids = prompt_and_ids
    try:
        answer, usage = client.json(SYSTEM, prompt, max_tokens=8000)
        items = answer.get("projects") if isinstance(answer, dict) else None
        if not isinstance(items, list):
            raise LLMError(f"no projects list in reply: {str(answer)[:150]}")
        tags = {}
        for item in items:
            n = int(_num(item.get("n"))) if isinstance(item, dict) else 0
            if 1 <= n <= len(ids):
                tags[ids[n - 1]] = normalise(item, len(ids))
        if len(tags) < len(ids):
            raise LLMError(f"reply covered {len(tags)} of {len(ids)} projects")
        return tags, _num(usage.get("cost")), None
    except (LLMError, ValueError, TypeError) as exc:
        return None, 0.0, str(exc)[:300]


def targets(conn, grades: str = "AB") -> list[tuple]:
    """(project row, hackathon_id, is_winner) for every full project page in an event with one
    of `grades`, or in any event before the first grading run. A project entered in several
    events is tagged once, against the first of them."""
    graded = conn.execute("SELECT 1 FROM event_labels_latest LIMIT 1").fetchone()
    marks = ", ".join("?" * len(grades))
    where = f"AND s.hackathon_id IN (SELECT hackathon_id FROM event_labels_latest WHERE grade IN ({marks}))"
    rows = conn.execute(
        f"SELECT p.*, MIN(s.hackathon_id) AS hid, MAX(s.is_winner) AS won FROM projects p "
        f"JOIN submissions s ON s.project_id = p.id WHERE p.detail_fetched = 1 {where if graded else ''} "
        f"GROUP BY p.id ORDER BY p.id", tuple(grades) if graded else ()).fetchall()
    return [(r, r["hid"], r["won"] or 0) for r in rows]


def _key(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


def batches(items: list[tuple]) -> list[tuple[int, list[tuple]]]:
    """Group (project, hackathon_id, won) by event, shuffle deterministically, cut into
    batches of up to BATCH. A remainder is spread so no batch is much smaller than the rest."""
    by_event: dict[int, list] = {}
    for t in items:
        by_event.setdefault(t[1], []).append(t)
    out = []
    for hid, rows in sorted(by_event.items()):
        rows.sort(key=lambda t: _key(t[0]["url"]))
        k = -(-len(rows) // BATCH)
        out += [(hid, rows[i::k]) for i in range(k)]
    return out


def pick_sample(conn, n: int, grades: str = "AB") -> list[tuple[int, list[tuple]]]:
    """Whole batches, deterministic, from events with both winners and non-winners, until
    about n projects: the comparison needs both in the same batch."""
    picked, total = [], 0
    for hid, rows in sorted(batches(targets(conn, grades)), key=lambda b: _key(str(b[0]))):
        if total >= n:
            break
        if len(rows) >= 3 and 0 < sum(w for *_, w in rows) < len(rows):
            picked.append((hid, rows))
            total += len(rows)
    return picked


def _combined(t: dict) -> int:
    return t["originality"] + t["technical_depth"] + t["polish"]


def separation(picked, tags: dict) -> tuple[float, float, int]:
    """How often a winner beats a non-winner from the same batch, by combined score and by
    rank (ties count half). 50% is a coin flip."""
    score = rank = pairs = 0.0
    for _, rows in picked:
        for (a, _, wa), (b, _, wb) in combinations(rows, 2):
            if wa == wb or a["id"] not in tags or b["id"] not in tags:
                continue
            w, l = (tags[a["id"]], tags[b["id"]]) if wa else (tags[b["id"]], tags[a["id"]])
            pairs += 1
            score += 1 if _combined(w) > _combined(l) else 0.5 if _combined(w) == _combined(l) else 0
            if w["batch_rank"] and l["batch_rank"]:
                rank += 1 if w["batch_rank"] < l["batch_rank"] else 0
            else:
                rank += 0.5
    return (score / pairs if pairs else 0.5), (rank / pairs if pairs else 0.5), int(pairs)


def _spread(tags: dict) -> str:
    vals = [v for t in tags.values() for v in (t["originality"], t["technical_depth"], t["polish"])]
    if not vals:
        return "-"
    return f"{min(vals)}-{max(vals)}, {sum(1 for v in vals if v <= 3 or v >= 8) * 100 // len(vals)}% at ≤3 or ≥8"


def sample(conn, data: Path, n: int, models: list[str], workers: int, max_cost: float,
           grades: str = "AB") -> Path:
    """Tag a sample with each model and write a side-by-side review file. No DB writes."""
    picked = pick_sample(conn, n, grades)
    jobs = [(i, (batch_prompt(conn, hid, [t[0] for t in rows]), [t[0]["id"] for t in rows]))
            for i, (hid, rows) in enumerate(picked)]
    count = sum(len(rows) for _, rows in picked)
    results, summary = {}, {}
    for model in models:
        prog = Progress(f"sample {model}", len(jobs))
        rows = list(_run_all(Client(model), jobs, workers, prog, max_cost, label=_tag))
        tags = {pid: t for _, lab, _, _ in rows if lab for pid, t in lab.items()}
        results[model] = tags
        summary[model] = (sum(c for _, _, c, _ in rows), sum(1 for *_, e in rows if e))
        prog.done()

    total = len(targets(conn, grades))
    winners = sum(w for _, rows in picked for *_, w in rows)
    lines = [f"# Project tags: {count} projects in {len(picked)} batches ({winners} winners)", "",
             "Per model: domain · AI role · form · sponsor tech · originality / depth / polish (0-10) · "
             "rank in batch · specific user · real use · summary. The models aren't told who won.",
             "**Winner beats non-winner** counts pairs from the same batch: 50% is a coin flip.", "",
             "| Model | Cost | Errors | Winner beats non-winner (score / rank) | Score spread | Projected full run |",
             "|---|---|---|---|---|---|"]
    for m, (c, e) in summary.items():
        s, r, pairs = separation(picked, results[m])
        lines.append(f"| {m} | ${c:.4f} | {e} of {len(jobs)} batches | {s:.0%} / {r:.0%} ({pairs} pairs) | "
                     f"{_spread(results[m])} | ${c / max(count, 1) * total:.2f} for {total:,} projects "
                     f"in {grades} events |")
    lines.append("")
    for hid, rows in picked:
        event = conn.execute("SELECT title FROM hackathons WHERE id=?", (hid,)).fetchone()[0]
        lines += [f"## {event}", ""]
        for p, _, won in rows:
            lines.append(f"**{p['title']}**{' (winner)' if won else ''} · {p['url']}")
            for model in models:
                t = results[model].get(p["id"])
                if t is None:
                    lines.append(f"- {model}: no tags")
                    continue
                lines.append(
                    f"- {model}: {t['domain']} · {t['ai_role']} · {t['form']} · sponsor {t['sponsor_tech']} · "
                    f"{t['originality']} / {t['technical_depth']} / {t['polish']} · "
                    f"rank {t['batch_rank'] or '?'} of {t['batch_size']} · "
                    f"{'user' if t['specific_user'] else 'no user'} · {'real use' if t['real_use'] else 'no use'} · "
                    f"{t['summary']}")
            lines.append("")
    out = data / "review" / f"projects-sample-{datetime.now():%Y%m%d-%H%M%S}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines))
    print(f"\nreview file: {out}")
    return out


def full_run(conn, model: str, workers: int, max_cost: float, snapshot_path: str | None,
             limit: int | None = None, grades: str = "AB") -> str:
    """Tag every target project. Resumes the newest unfinished run with the same model,
    prompt and grades; otherwise starts a new run (earlier runs are kept). `limit` caps the
    number of batches this run."""
    params = json.dumps({"model": model, "prompt": PROMPT_VERSION, "grades": grades})
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

    def tagged():
        return {r[0] for r in conn.execute("SELECT project_id FROM project_tags WHERE run_id=? AND error IS NULL",
                                           (run_id,))}

    every = targets(conn, grades)
    done = tagged()
    todo = batches([t for t in every if t[0]["id"] not in done])[:limit]
    jobs = [((hid, tuple(t[0]["id"] for t in rows)),
             (batch_prompt(conn, hid, [t[0] for t in rows]), [t[0]["id"] for t in rows])) for hid, rows in todo]
    prog = Progress("tag projects", len(jobs))
    errors = 0
    for (hid, ids), lab, cost, error in _run_all(Client(model), jobs, workers, prog, max_cost, label=_tag):
        errors += bool(error)
        for pid in ids:
            t = (lab or {}).get(pid, {})
            row = {"run_id": run_id, "project_id": pid, "hackathon_id": hid, "cost": cost / len(ids),
                   "error": error, **{k: t.get(k) for k in TAG_COLUMNS}}
            conn.execute(f"INSERT OR REPLACE INTO project_tags ({', '.join(row)}) "
                         f"VALUES ({', '.join('?' * len(row))})", list(row.values()))
        conn.commit()
    prog.done()
    done = tagged()
    left = sum(1 for p, *_ in every if p["id"] not in done)
    if left == 0:
        conn.execute("UPDATE enrich_runs SET finished_at=? WHERE run_id=?",
                     (datetime.now(timezone.utc).isoformat(), run_id))
        conn.commit()
    cost = conn.execute("SELECT SUM(cost) FROM project_tags WHERE run_id=?", (run_id,)).fetchone()[0] or 0
    print(f"{run_id}: {len(done):,} projects tagged · ${cost:.2f} · {errors} failed batches"
          + (f" · {left:,} left, re-run to finish" if left else ""))
    return run_id
