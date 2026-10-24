"""LLM labels per event: what the prizes are really worth, and whether the event is legit.

One call per event reads every prize block plus the organizer, judges and field size, and
returns cash, credits, products and opportunities separately. That way a Logitech hackathon
paying in real hardware, or a Google one offering time with their team, isn't mistaken for
swag-only, and a "$40k participation prize" isn't mistaken for cash.

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

PROMPT_VERSION = "1"
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
COMPARE_MODELS = ["deepseek/deepseek-v4.1-flash", "z-ai/glm-5.3-flash", "anthropic/claude-haiku-5.5"]

SYSTEM = """You assess hackathons for a researcher studying what wins. From the event details,
work out what the prizes are really worth and whether the event is a real, worthwhile competition.
Answer with one JSON object and nothing else:
{
  "cash_usd": total money paid to winners (cash, bank transfer, PayPal, stablecoins), in USD,
  "gift_cards_usd": total gift cards, in USD,
  "credits_usd": cloud/API/platform credits, vouchers, subscriptions, licences, domains, in USD,
  "products_usd": physical products (laptops, headphones, dev kits...) at estimated retail, in USD,
  "crypto_tokens_usd": prizes paid in a project's own token (not stablecoins), in USD,
  "opportunities": short list such as "meeting with Google Cloud team", "accelerator interview",
  "opportunity_strength": 0 none, 1 weak (certificate, shoutout, swag), 2 real (interviews,
      mentoring, demo day, pitching to investors), 3 strong (investment, accelerator place,
      job offer track, time with a major company's product team),
  "sponsor_known": 0 unknown or unverifiable, 1 real but niche, 2 major well-known company,
      university or institution,
  "legit": 0-100 confidence this is a real, fairly run competition worth studying,
  "inflated": true if headline amounts are padded (per-participant pools, "equivalent value",
      totals that count credits or swag as cash),
  "reason": one short sentence
}
Count each prize once; when a prize has several winners, multiply. Convert other currencies
to USD. Use 0 when a kind of reward is absent. Do not count the same money twice when the
title and description repeat it."""


def event_prompt(conn, h) -> str:
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
        desc = " ".join((p["description"] or "").split())[:500]
        winners = f" · {p['winners_count']} winners" if (p["winners_count"] or 1) > 1 else ""
        lines.append(f"- {p['title']} [{p['value_text'] or 'no value given'}{winners}] {desc}")
    judges = conn.execute("SELECT name, title FROM judges WHERE hackathon_id=? LIMIT 8", (h["id"],)).fetchall()
    if judges:
        lines += ["", "Judges: " + "; ".join(f"{j['name']} ({j['title'] or '-'})" for j in judges)]
    return "\n".join(lines)


def _num(v) -> float:
    try:
        return max(0.0, float(v or 0))
    except (TypeError, ValueError):
        return 0.0


def normalise(a: dict) -> dict:
    out = {k: _num(a.get(k)) for k in
           ("cash_usd", "gift_cards_usd", "credits_usd", "products_usd", "crypto_tokens_usd")}
    opps = a.get("opportunities") or []
    out["opportunities"] = [str(o) for o in opps] if isinstance(opps, list) else [str(opps)]
    out["opportunity_strength"] = int(min(3, _num(a.get("opportunity_strength"))))
    out["sponsor_known"] = int(min(2, _num(a.get("sponsor_known"))))
    out["legit"] = int(min(100, _num(a.get("legit"))))
    out["inflated"] = int(bool(a.get("inflated")))
    out["reason"] = str(a.get("reason") or "")[:300]
    out["verdict"] = verdict(out)
    return out


def verdict(x: dict) -> str:
    """worth | maybe | skip. Any meaningful reward counts, not just cash."""
    money = x["cash_usd"] + x["gift_cards_usd"] + 0.5 * x["crypto_tokens_usd"]
    meaningful = (money >= 1000 or x["products_usd"] >= 1000
                  or (x["opportunity_strength"] >= 2 and x["sponsor_known"] == 2)
                  or x["opportunity_strength"] == 3)
    if x["legit"] < 40 or not meaningful:
        return "skip"
    return "worth" if x["legit"] >= 70 else "maybe"


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


def sample(conn, data: Path, n: int, models: list[str], workers: int, max_cost: float) -> Path:
    """Label a sample with each model and write a side-by-side review file. No DB writes."""
    events = pick_sample(conn, n)
    tiers = {r["hackathon_id"]: r["tier"] for r in conn.execute("SELECT hackathon_id, tier FROM event_scores_latest")}
    prompts = [(h["id"], event_prompt(conn, h)) for h in events]
    results: dict[str, dict] = {}
    for model in models:
        prog = Progress(f"sample {model}", len(prompts))
        results[model] = {k: (lab, err) for k, lab, _, err in
                          _run_all(Client(model), prompts, workers, prog, max_cost)}
        prog.done()

    out = data / "review" / f"events-sample-{datetime.now():%Y%m%d-%H%M%S}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Event labels: sample of {len(events)}", "",
             "Columns per model: verdict · legit · cash / products / credits (USD) · opportunity strength · sponsor known.",
             "Mark the ones that look wrong; that decides the model.", ""]
    for h in events:
        lines += [f"## {h['title']}", f"{h['url']} · organizer: {h['organizer']} · "
                  f"{h['submissions_count']} submissions · old tier: {tiers.get(h['id'], '-')} · "
                  f"old cash estimate: ${h['cash_usd_est'] or 0:,.0f}", ""]
        for model in models:
            lab, err = results[model].get(h["id"], (None, "not run"))
            if lab is None:
                lines.append(f"- **{model}**: error: {err}")
                continue
            lines.append(
                f"- **{model}**: **{lab['verdict']}** · legit {lab['legit']} · cash ${lab['cash_usd']:,.0f} / "
                f"products ${lab['products_usd']:,.0f} / credits ${lab['credits_usd']:,.0f} · "
                f"opp {lab['opportunity_strength']} · sponsor {lab['sponsor_known']} · {lab['reason']}"
                + (f" · opportunities: {'; '.join(lab['opportunities'][:3])}" if lab["opportunities"] else ""))
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
        conn.execute(
            "INSERT OR REPLACE INTO event_labels VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, hid, lab.get("cash_usd"), lab.get("gift_cards_usd"), lab.get("credits_usd"),
             lab.get("products_usd"), lab.get("crypto_tokens_usd"), json.dumps(lab.get("opportunities", [])),
             lab.get("opportunity_strength"), lab.get("sponsor_known"), lab.get("legit"), lab.get("inflated"),
             lab.get("reason"), lab.get("verdict"), cost, error))
        conn.commit()
    prog.done()
    left = conn.execute("SELECT COUNT(*) FROM hackathons WHERE submissions_count > 0 AND id NOT IN "
                        "(SELECT hackathon_id FROM event_labels WHERE run_id=? AND error IS NULL)", (run_id,)).fetchone()[0]
    if left == 0:
        conn.execute("UPDATE enrich_runs SET finished_at=? WHERE run_id=?",
                     (datetime.now(timezone.utc).isoformat(), run_id))
        conn.commit()
    counts = dict(conn.execute("SELECT verdict, COUNT(*) FROM event_labels WHERE run_id=? AND error IS NULL "
                               "GROUP BY verdict", (run_id,)).fetchall())
    cost = conn.execute("SELECT SUM(cost) FROM event_labels WHERE run_id=?", (run_id,)).fetchone()[0] or 0
    print(f"{run_id}: " + ", ".join(f"{counts.get(v, 0)} {v}" for v in ("worth", "maybe", "skip"))
          + f" · ${cost:.2f} · {errors} errors" + (f" · {left} left, re-run to finish" if left else ""))
    return run_id
