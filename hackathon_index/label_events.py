"""LLM grades per event: how much there is to learn from it, 0-100.

One call per event reads every prize block, the organizer, judges and field size, and the
top winners' writeups. It returns what the prizes are really worth (cash, credits, products
and opportunities separately, so real hardware isn't mistaken for swag and a "$40k
participation prize" isn't mistaken for cash) and four 0-10 scores: winner quality, stakes,
sponsor caliber and competition. A fixed weighting turns those into the quality score and an
A-D grade. Legit isn't enough: a well-run local weekend with stock partner prizes grades low.

    label-events --sample 30 --compare a,b,c   test: review file only, the database is untouched
    label-events --model M                     full run: snapshot first, then a new versioned run
"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from .db import new_run_id
from .llm import Client, LLMError
from .progress import Progress

PROMPT_VERSION = "2"  # 1: worth/maybe/skip from prizes; 2: 0-100 quality score incl. winner quality
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
COMPARE_MODELS = ["deepseek/deepseek-v4.1-flash", "z-ai/glm-5.3-flash", "anthropic/claude-haiku-5.5"]
EVENT_TYPES = ("student_local", "student_online", "community", "company_challenge", "professional_open")

SYSTEM = """You grade past hackathons for a builder who wants to learn what wins strong, online,
2026-level competitions. A legit event is not enough: a local student weekend whose prizes are
stock partner swag teaches little, even if it was run well. Read the event, its prizes and its
top winners, and answer with one JSON object and nothing else:
{
  "cash_usd": money paid to winners (cash, bank transfer, PayPal, stablecoins), in USD,
  "gift_cards_usd": gift cards, in USD,
  "credits_usd": cloud/API/platform credits, vouchers, subscriptions, licences, domains, in USD,
  "products_usd": physical products at estimated retail, in USD,
  "crypto_tokens_usd": prizes paid in a project's own token (not stablecoins), in USD,
  "opportunities": short list, e.g. "meeting with Google Cloud team", "accelerator interview",
  "event_type": one of "student_local", "student_online", "community", "company_challenge",
      "professional_open",
  "stock_prizes": true if most prize categories are generic partner prizes (MLH partner tracks
      such as GoDaddy domains, TinyMCE, Kintone, "Best use of X" with small swag) rather than
      prizes the organizer or a major sponsor designed and funded,
  "stakes": 0-10, how much winning is worth: real value of the top prizes and how concentrated
      they are (a few large prizes beat twenty token awards); credits and swag count little,
  "sponsor_caliber": 0-10, how significant the companies funding and judging the main prizes are
      (10 = Google, AWS, Microsoft, OpenAI-level running their own track; 2 = small tool sponsors),
  "competition": 0-10, how hard it was to win: field size, open online vs local, professional
      vs student entrants,
  "winner_quality": 0-10, how impressive the winning projects below are by today's standards
      for an online hackathon: ambition, technical depth, polish, real use of sponsor tech
      (10 = would stand out at a top online challenge in 2026; 3 = a standard weekend demo),
  "winner_notes": one sentence on the winners,
  "legit": 0-100 confidence this is a real, fairly run competition,
  "inflated": true if headline amounts are padded (per-participant pools, "equivalent value",
      credits or swag counted as cash),
  "reason": one short sentence explaining the overall grade
}
Count each prize once; when a prize has several winners, multiply. Convert other currencies
to USD. Use 0 when a kind of reward is absent. Judge winners on what the writeups show; don't
reward length or buzzwords alone."""


def event_prompt(conn, h, winners: int = 3, writeup_chars: int = 1200) -> str:
    themes = ", ".join(json.loads(h["themes"] or "[]"))
    lines = [
        f"Title: {h['title']}",
        f"Organizer: {h['organizer'] or 'unknown'}",
        f"Dates: {h['start_date']} to {h['end_date']} · {'online' if h['is_online'] else h['location'] or ''}",
        f"Themes: {themes}",
        f"Registrants: {h['registrants']} · Submissions: {h['submissions_count']}",
        f"Headline prize (from the listing): {h['headline_prize_text']}",
        "", "Prizes:",
    ]
    prizes = conn.execute("SELECT title, value_text, winners_count, description FROM prizes "
                          "WHERE hackathon_id=? ORDER BY position LIMIT 30", (h["id"],)).fetchall()
    for p in prizes:
        desc = " ".join((p["description"] or "").split())[:400]
        count = f" · {p['winners_count']} winners" if (p["winners_count"] or 1) > 1 else ""
        lines.append(f"- {p['title']} [{p['value_text'] or 'no value given'}{count}] {desc}")
    judges = conn.execute("SELECT name, title FROM judges WHERE hackathon_id=? LIMIT 8", (h["id"],)).fetchall()
    if judges:
        lines += ["", "Judges: " + "; ".join(f"{j['name']} ({j['title'] or '-'})" for j in judges)]

    top = conn.execute(
        "SELECT p.id, p.title, p.tagline, p.built_with, p.has_repo, p.has_video, p.writeup FROM submissions s "
        "JOIN projects p ON p.id = s.project_id WHERE s.hackathon_id=? AND s.is_winner=1 "
        "AND p.detail_fetched=1 AND p.writeup_chars > 200 ORDER BY s.gallery_rank LIMIT ?",
        (h["id"], winners)).fetchall()
    if top:
        lines += ["", "Top winners:"]
    for w in top:
        won = [r[0] for r in conn.execute("SELECT prize_title FROM submission_prizes WHERE hackathon_id=? "
                                          "AND project_id=?", (h["id"], w["id"]))]
        text = " ".join(" ".join(f"{k}: {v}" for k, v in json.loads(w["writeup"] or "{}").items()).split())
        built = ", ".join(json.loads(w["built_with"] or "[]")[:12])
        lines += [f"* {w['title']}: {w['tagline'] or ''}",
                  f"  won: {'; '.join(won) or '?'} · built with: {built or '-'} · "
                  f"repo: {'yes' if w['has_repo'] else 'no'} · video: {'yes' if w['has_video'] else 'no'}",
                  f"  {text[:writeup_chars]}"]
    return "\n".join(lines)


def _num(v) -> float:
    try:
        return max(0.0, float(v or 0))
    except (TypeError, ValueError):
        return 0.0


def _score10(v) -> int:
    return int(round(min(10, _num(v))))


def normalise(a: dict) -> dict:
    out = {k: _num(a.get(k)) for k in
           ("cash_usd", "gift_cards_usd", "credits_usd", "products_usd", "crypto_tokens_usd")}
    opps = a.get("opportunities") or []
    out["opportunities"] = [str(o) for o in opps] if isinstance(opps, list) else [str(opps)]
    out["event_type"] = a.get("event_type") if a.get("event_type") in EVENT_TYPES else None
    out["stock_prizes"] = int(bool(a.get("stock_prizes")))
    for k in ("stakes", "sponsor_caliber", "competition", "winner_quality"):
        out[k] = _score10(a.get(k))
    out["winner_notes"] = str(a.get("winner_notes") or "")[:300]
    out["legit"] = int(min(100, _num(a.get("legit"))))
    out["inflated"] = int(bool(a.get("inflated")))
    out["reason"] = str(a.get("reason") or "")[:300]
    out["quality"] = quality(out)
    out["grade"] = grade(out["quality"])
    out["verdict"] = {"A": "worth", "B": "worth", "C": "maybe"}.get(out["grade"], "skip")
    return out


WEIGHTS = {"winner_quality": 0.35, "stakes": 0.25, "sponsor_caliber": 0.20, "competition": 0.20}


def quality(x: dict) -> int:
    """0-100. Winner quality weighs most: it's what the researcher actually learns from."""
    q = 10 * sum(x[k] * w for k, w in WEIGHTS.items())
    if x["legit"] < 40:
        q = min(q, 20)
    if x["stock_prizes"]:
        q -= 5
    return int(round(max(0, min(100, q))))


def grade(q: int) -> str:
    return "A" if q >= 75 else "B" if q >= 60 else "C" if q >= 40 else "D"


LABEL_COLUMNS = ("cash_usd", "gift_cards_usd", "credits_usd", "products_usd", "crypto_tokens_usd",
                 "opportunities", "event_type", "stock_prizes", "stakes", "sponsor_caliber", "competition",
                 "winner_quality", "winner_notes", "legit", "inflated", "reason", "quality", "grade", "verdict")


def _label(client: Client, prompt: str) -> tuple[dict | None, float, str | None]:
    try:
        answer, usage = client.json(SYSTEM, prompt)
        return normalise(answer), _num(usage.get("cost")), None
    except (LLMError, ValueError) as exc:
        return None, 0.0, str(exc)[:300]


def _run_all(client: Client, jobs: list[tuple], workers: int, prog: Progress, max_cost: float):
    """jobs: (key, prompt). Yields (key, labels, cost, error); stops once max_cost is spent."""
    spent = 0.0
    with ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(_label, client, prompt): key for key, prompt in jobs}
        for fut in as_completed(futures):
            labels, cost, error = fut.result()
            spent += cost
            prog.step(f"${spent:.3f} spent" + (f" · error: {error[:60]}" if error else ""))
            yield futures[fut], labels, cost, error
            if spent > max_cost:
                print(f"\nstopping: spent ${spent:.2f}, over --max-cost {max_cost}. Re-run to continue.")
                for f in futures:
                    f.cancel()
                return


def _targets(conn):
    return conn.execute("SELECT * FROM hackathons WHERE submissions_count > 0 ORDER BY end_date DESC").fetchall()


def pick_sample(conn, n: int) -> list:
    """Deterministic and spread across the current junk-score tiers, so the review covers
    clear keeps, borderline events and likely junk."""
    tiers = {r["hackathon_id"]: r["tier"] for r in conn.execute("SELECT hackathon_id, tier FROM event_scores_latest")}
    by_tier: dict[str, list] = {}
    for h in _targets(conn):
        by_tier.setdefault(tiers.get(h["id"], "unscored"), []).append(h)
    for rows in by_tier.values():
        rows.sort(key=lambda h: hashlib.sha1(h["url"].encode()).hexdigest())
    picked, i = [], 0
    while len(picked) < n and any(i < len(r) for r in by_tier.values()):
        for t in sorted(by_tier):
            if i < len(by_tier[t]) and len(picked) < n:
                picked.append(by_tier[t][i])
        i += 1
    return picked


def load_anchors(path: Path) -> list[tuple[str, str]]:
    """anchors.txt: one event per line, `+` for great and `-` for meh, then a URL or a title.
    Anchors always join the sample, so the grades can be checked against your own judgement."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line[:1] in "+-" and line[1:].strip():
            out.append(("great" if line[0] == "+" else "meh", line[1:].strip()))
    return out


def _find(conn, ref: str):
    if "devpost.com" in ref:
        return conn.execute("SELECT * FROM hackathons WHERE url LIKE ?", (f"%{ref.split('//')[-1].split('/')[0]}%",)).fetchone()
    return conn.execute("SELECT * FROM hackathons WHERE title LIKE ? ORDER BY submissions_count DESC",
                        (f"%{ref}%",)).fetchone()


def sample(conn, data: Path, n: int, models: list[str], workers: int, max_cost: float,
           anchors_path: Path = Path("anchors.txt")) -> Path:
    """Label a sample with each model and write a side-by-side review file. No DB writes."""
    expected, events = {}, []
    for label, ref in load_anchors(anchors_path):
        h = _find(conn, ref)
        if h is None:
            print(f"  anchor not found in the database: {ref}")
        elif h["id"] not in expected:
            expected[h["id"]] = label
            events.append(h)
    events += [h for h in pick_sample(conn, n) if h["id"] not in expected]
    tiers = {r["hackathon_id"]: r["tier"] for r in conn.execute("SELECT hackathon_id, tier FROM event_scores_latest")}
    prompts = [(h["id"], event_prompt(conn, h)) for h in events]
    results: dict[str, dict] = {}
    summary: dict[str, tuple[float, int]] = {}
    for model in models:
        prog = Progress(f"sample {model}", len(prompts))
        rows = list(_run_all(Client(model), prompts, workers, prog, max_cost))
        results[model] = {k: (lab, err) for k, lab, _, err in rows}
        summary[model] = (sum(c for _, _, c, _ in rows), sum(1 for *_, e in rows if e))
        prog.done()

    def agreement(model):
        ok = sum(1 for hid, label in expected.items()
                 if (lab := results[model].get(hid, (None,))[0])
                 and (lab["grade"] in "AB") == (label == "great"))
        return f"{ok} of {len(expected)}" if expected else "-"

    out = data / "review" / f"events-sample-{datetime.now():%Y%m%d-%H%M%S}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Event grades: {len(events)} events ({len(expected)} of them your anchors)", "",
             "Per model: **grade** (quality 0-100) · winners / stakes / sponsor / competition (0-10) · "
             "event type · cash / products / credits (USD) · reason.",
             "Anchors agree when great → A/B and meh → C/D.", "",
             "| Model | Cost | Errors | Anchors agreeing |", "|---|---|---|---|"]
    lines += [f"| {m} | ${c:.4f} | {e} of {len(events)} | {agreement(m)} |" for m, (c, e) in summary.items()]
    lines.append("")
    for h in events:
        anchor = f" · **your anchor: {expected[h['id']]}**" if h["id"] in expected else ""
        lines += [f"## {h['title']}", f"{h['url']} · organizer: {h['organizer']} · "
                  f"{h['submissions_count']} submissions · old tier: {tiers.get(h['id'], '-')}{anchor}", ""]
        for model in models:
            lab, err = results[model].get(h["id"], (None, "not run"))
            if lab is None:
                lines.append(f"- **{model}**: error: {err}")
                continue
            lines.append(
                f"- **{model}**: **{lab['grade']}** ({lab['quality']}) · winners {lab['winner_quality']} / "
                f"stakes {lab['stakes']} / sponsor {lab['sponsor_caliber']} / competition {lab['competition']} · "
                f"{lab['event_type'] or '?'}{' · stock prizes' if lab['stock_prizes'] else ''} · "
                f"cash ${lab['cash_usd']:,.0f} / products ${lab['products_usd']:,.0f} / "
                f"credits ${lab['credits_usd']:,.0f} · {lab['reason']} Winners: {lab['winner_notes']}")
        lines.append("")
    out.write_text("\n".join(lines))
    print(f"\nreview file: {out}")
    return out


def full_run(conn, model: str, workers: int, max_cost: float, snapshot_path: str | None,
             limit: int | None = None) -> str:
    """Label every gallery event. Resumes the newest unfinished run with the same model and
    prompt; otherwise starts a new run (earlier runs are kept)."""
    params = json.dumps({"model": model, "prompt": PROMPT_VERSION})
    row = conn.execute("SELECT run_id FROM enrich_runs WHERE step='events' AND params=? "
                       "AND finished_at IS NULL ORDER BY created_at DESC LIMIT 1", (params,)).fetchone()
    if row:
        run_id = row[0]
        print(f"resuming {run_id}")
    else:
        now = datetime.now(timezone.utc)
        run_id = new_run_id(conn, "events", now)
        conn.execute("INSERT INTO enrich_runs (run_id, step, rules, params, snapshot, created_at) "
                     "VALUES (?, 'events', ?, ?, ?, ?)", (run_id, PROMPT_VERSION, params, snapshot_path, now.isoformat()))
        conn.commit()
    done = {r[0] for r in conn.execute("SELECT hackathon_id FROM event_labels WHERE run_id=? AND error IS NULL", (run_id,))}
    todo = [h for h in _targets(conn) if h["id"] not in done][:limit]
    prog = Progress("label events", len(todo))
    client, errors = Client(model), 0
    for hid, lab, cost, error in _run_all(client, [(h["id"], event_prompt(conn, h)) for h in todo],
                                         workers, prog, max_cost):
        lab = lab or {}
        errors += bool(error)
        row = {"run_id": run_id, "hackathon_id": hid, "cost": cost, "error": error,
               **{k: lab.get(k) for k in LABEL_COLUMNS}}
        row["opportunities"] = json.dumps(lab.get("opportunities", []))
        conn.execute(f"INSERT OR REPLACE INTO event_labels ({', '.join(row)}) "
                     f"VALUES ({', '.join('?' * len(row))})", list(row.values()))
        conn.commit()
    prog.done()
    left = conn.execute("SELECT COUNT(*) FROM hackathons WHERE submissions_count > 0 AND id NOT IN "
                        "(SELECT hackathon_id FROM event_labels WHERE run_id=? AND error IS NULL)", (run_id,)).fetchone()[0]
    if left == 0:
        conn.execute("UPDATE enrich_runs SET finished_at=? WHERE run_id=?",
                     (datetime.now(timezone.utc).isoformat(), run_id))
        conn.commit()
    counts = dict(conn.execute("SELECT grade, COUNT(*) FROM event_labels WHERE run_id=? AND error IS NULL "
                               "GROUP BY grade", (run_id,)).fetchall())
    cost = conn.execute("SELECT SUM(cost) FROM event_labels WHERE run_id=?", (run_id,)).fetchone()[0] or 0
    print(f"{run_id}: grades " + ", ".join(f"{g} {counts.get(g, 0)}" for g in "ABCD")
          + f" · ${cost:.2f} · {errors} errors" + (f" · {left} left, re-run to finish" if left else ""))
    return run_id
