"""CLI.  python -m hackathon_index <command> [options]

    crawl   run one Devpost stage, or all of them in order
    status  row counts and a quick look at the biggest events
"""

import argparse
from pathlib import Path

from .db import connect
from .fetcher import Fetcher
from .sources.devpost import crawl

STAGES = ("list", "overview", "gallery", "projects")


def main() -> None:
    ap = argparse.ArgumentParser(prog="hackathon_index")
    ap.add_argument("--data", type=Path, default=Path("data"), help="folder for index.db and raw pages")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("crawl")
    c.add_argument("stage", choices=(*STAGES, "all"))
    c.add_argument("--since", default="2024-01-01", help="keep events whose submissions closed on/after this")
    c.add_argument("--min-cash", type=float, default=1000, help="USD cash needed to crawl the gallery")
    c.add_argument("--limit", type=int, help="only the N most recent qualifying hackathons (pilot runs)")
    c.add_argument("--max-list-pages", type=int, help="stop the listing early (pilot runs)")
    c.add_argument("--loser-rate", type=float, default=0.15, help="share of non-winners to fetch in full")
    c.add_argument("--min-losers", type=int, default=5, help="at least this many non-winners per event")
    c.add_argument("--interval", type=float, default=4.0, help="seconds between requests (plus jitter)")

    sub.add_parser("status")
    args = ap.parse_args()

    conn = connect(args.data / "index.db")
    if args.cmd == "status":
        return status(conn)

    fetcher = Fetcher(conn, args.data / "raw", min_interval=args.interval)
    stages = STAGES if args.stage == "all" else (args.stage,)
    try:
        for stage in stages:
            if stage == "list":
                crawl.crawl_list(conn, fetcher, args.since, args.max_list_pages)
            elif stage == "overview":
                crawl.crawl_overviews(conn, fetcher, args.limit)
            elif stage == "gallery":
                crawl.crawl_galleries(conn, fetcher, args.min_cash, args.limit)
            elif stage == "projects":
                crawl.crawl_projects(conn, fetcher, args.loser_rate, args.min_losers, args.limit)
    except KeyboardInterrupt:
        print("\nstopped; everything fetched so far is saved. Re-run the same command to resume.")
    status(conn)


def status(conn) -> None:
    q = lambda sql: conn.execute(sql).fetchone()[0]
    print(
        f"\nhackathons {q('SELECT COUNT(*) FROM hackathons')}"
        f" | with prizes parsed {q('SELECT COUNT(*) FROM hackathons WHERE cash_usd_est IS NOT NULL')}"
        f" | galleries {q('SELECT COUNT(*) FROM hackathons WHERE submissions_count > 0')}"
        f"\nprojects {q('SELECT COUNT(*) FROM projects')}"
        f" | winners {q('SELECT COUNT(*) FROM submissions WHERE is_winner = 1')}"
        f" | full pages {q('SELECT COUNT(*) FROM projects WHERE detail_fetched = 1')}"
        f" | pages cached {q('SELECT COUNT(*) FROM pages WHERE status = 200')}"
        f" | failed {q('SELECT COUNT(*) FROM pages WHERE status IS NULL OR status != 200')}"
    )


if __name__ == "__main__":
    main()
