"""Junk scoring writes a new run each time and never edits the crawl tables; snapshots
and restore make every earlier state recoverable."""

import json

from hackathon_index import snapshot
from hackathon_index.db import connect
from hackathon_index.score import flags_for, score_events, tier


def event(**kw):
    e = {"cash_usd_est": 5000, "submissions_count": 80, "registrants": 400, "judges_count": 6,
         "managed_by_source": 0}
    e.update(kw)
    return e


def test_flags():
    assert flags_for(event(), 3, [(2500, 1)] * 5) == {}
    assert tier(sum(flags_for(event(cash_usd_est=0), 3, []).values())) == "junk"   # swag only
    bogus = flags_for(event(cash_usd_est=10000, submissions_count=2, registrants=10), 1, [(200, 0)])
    assert {"tiny_field", "few_registrants", "prize_per_entry_absurd", "one_off_organizer"} <= set(bogus)
    assert tier(sum(bogus.values())) == "junk"
    trusted = flags_for(event(submissions_count=15, managed_by_source=1), 8, [(2500, 1)])
    assert tier(sum(trusted.values())) == "ok"


def test_runs_are_kept_and_snapshots_restore(tmp_path):
    db = tmp_path / "index.db"
    conn = connect(db)
    conn.execute("INSERT INTO hackathons (id, source, source_id, url, organizer, submissions_count, "
                 "registrants, cash_usd_est, judges_count) VALUES (1, 'devpost', '1', 'u', 'Org', 3, 9, 0, 0)")
    conn.commit()
    first = score_events(conn, str(snapshot.take(db, "before-score")))
    conn.execute("UPDATE hackathons SET cash_usd_est = 50000")
    conn.commit()
    second = score_events(conn)
    rows = conn.execute("SELECT run_id, tier FROM event_scores ORDER BY run_id").fetchall()
    assert [r["run_id"] for r in rows] == [first, second]                 # both runs kept
    assert conn.execute("SELECT run_id FROM event_scores_latest").fetchone()[0] == second
    assert json.loads(conn.execute("SELECT flags FROM event_scores WHERE run_id=?", (first,))
                      .fetchone()[0])["no_cash"] == 4
    conn.close()

    name = snapshot.listing(db)[0].name
    snapshot.restore(db, name)
    conn = connect(db)
    assert conn.execute("SELECT cash_usd_est FROM hackathons").fetchone()[0] == 0   # back to before
    assert conn.execute("SELECT COUNT(*) FROM event_scores").fetchone()[0] == 0
    assert len(snapshot.listing(db)) == 2                                  # restore saved the old state too
