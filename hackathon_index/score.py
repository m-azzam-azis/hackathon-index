"""Junk-event scoring: rule-based flags over the crawled data, no network.

Each flag adds its weight to the score; trust signals subtract. Tiers: ok (score <= 1),
watch (2-3), junk (>= 4). Results go to `event_scores` under a new run_id, so every run is
kept and the rules can be tuned and compared. Nothing is deleted or excluded here: the
tier is a label that analysis queries filter on.
"""

import json
import statistics
from datetime import datetime, timezone

RULES = "1"


def flags_for(e: dict, org_events: int, winners: list[tuple[int, int]]) -> dict[str, int]:
    """`winners`: (writeup_chars, has_repo) for each winner with a full page."""
    f: dict[str, int] = {}
    cash, subs, regs = e["cash_usd_est"] or 0, e["submissions_count"] or 0, e["registrants"] or 0
    if cash == 0:
        f["no_cash"] = 4                  # only swag, gift cards or credits found in the prizes
    elif cash < 1000:
        f["low_cash"] = 2
    if subs < 10:
        f["tiny_field"] = 3
    elif subs < 20:
        f["small_field"] = 1
    if regs < 30:
        f["few_registrants"] = 2
    if cash and subs < 20 and cash / max(subs, 1) > 2500:
        f["prize_per_entry_absurd"] = 2   # big money, almost nobody entered
    if not e["judges_count"]:
        f["no_judges"] = 1
    if org_events <= 1 and not e["managed_by_source"]:
        f["one_off_organizer"] = 1
    if winners:
        if statistics.median(w for w, _ in winners) < 800:
            f["weak_winner_writeups"] = 1
        if sum(r for _, r in winners) / len(winners) < 0.25:
            f["winners_without_code"] = 1
    if e["managed_by_source"]:
        f["managed_by_devpost"] = -2
    if org_events >= 5:
        f["repeat_organizer"] = -1
    return f


def tier(score: int) -> str:
    return "ok" if score <= 1 else "watch" if score <= 3 else "junk"


def score_events(conn, snapshot: str | None = None) -> str:
    now = datetime.now(timezone.utc)
    run_id = f"score-{now:%Y%m%d-%H%M%S}"
    conn.execute("INSERT INTO enrich_runs VALUES (?, 'score', ?, ?, ?, ?)",
                 (run_id, RULES, json.dumps({}), snapshot, now.isoformat()))

    org = {r[0]: r[1] for r in conn.execute(
        "SELECT LOWER(TRIM(organizer)), COUNT(*) FROM hackathons GROUP BY 1")}
    winners: dict[int, list[tuple[int, int]]] = {}
    for h, w, r in conn.execute(
            "SELECT s.hackathon_id, p.writeup_chars, p.has_repo FROM submissions s "
            "JOIN projects p ON p.id = s.project_id WHERE s.is_winner = 1 AND p.detail_fetched = 1"):
        winners.setdefault(h, []).append((w or 0, r or 0))

    counts = {"ok": 0, "watch": 0, "junk": 0}
    for e in conn.execute("SELECT * FROM hackathons WHERE submissions_count > 0").fetchall():
        e = dict(e)
        f = flags_for(e, org.get((e["organizer"] or "").strip().lower(), 0), winners.get(e["id"], []))
        s = sum(f.values())
        counts[tier(s)] += 1
        reason = max(f, key=f.get) if f and max(f.values()) > 0 else None
        conn.execute("INSERT INTO event_scores VALUES (?, ?, ?, ?, ?, ?)",
                     (run_id, e["id"], s, tier(s), reason, json.dumps(f)))
    conn.commit()
    print(f"{run_id}: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    return run_id
