"""CLI.  python -m hackathon_index <command> [options]

    crawl   run one Devpost stage, or all of them in order
    status  row counts and a quick look at the biggest events
"""

import argparse
import fcntl
import sys
from pathlib import Path

from .db import connect
from .fetcher import CrawlHalted, Fetcher
from .sources.devpost import crawl

STAGES = ("list", "overview", "gallery", "categories", "projects")


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
    c.add_argument("--refresh-days", type=int, default=60,
                   help="re-fetch listing pages holding events that ended within this many days")
    c.add_argument("--loser-rate", type=float, default=0.0, help="share of non-winners to fetch in full (0 = winners only)")
    c.add_argument("--min-losers", type=int, default=0, help="at least this many non-winners per event")
    c.add_argument("--max-losers", type=int, default=40, help="at most this many non-winners per event")
    c.add_argument("--top-liked", type=int, default=10, help="of the sampled non-winners, this many are the most-liked")
    c.add_argument("--min-submissions", type=int, default=10,
                   help="skip the category and non-winner passes for events smaller than this")
    c.add_argument("--category-pages", type=int, default=3, help="filtered gallery pages read per category")
    c.add_argument("--categories", action="store_true",
                   help="run the category pass in 'all' even without --loser-rate")
    c.add_argument("--workers", type=int, default=3, help="parallel requests (1 = sequential)")
    c.add_argument("--interval", type=float, default=0.5, help="seconds between requests per worker (plus jitter)")
    c.add_argument("--reparse", action="store_true", help="re-parse already processed pages (from cache)")

    sub.add_parser("status")
    args = ap.parse_args()

    conn = connect(args.data / "index.db")
    if args.cmd == "status":
        return status(conn)

    lock = open(args.data / ".crawl.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("another crawl is already running on this data folder; wait for it to finish")

    # The fetcher gets its own connection: worker threads share it under the fetcher's lock.
    fetcher = Fetcher(connect(args.data / "index.db", threads=True), args.data / "raw",
                      min_interval=args.interval, workers=args.workers)
    if args.stage == "all":
        # The category pass only matters for picking non-winners, so it rides along with them.
        stages = [s for s in STAGES if s != "categories" or args.loser_rate > 0 or args.categories]
    else:
        stages = [args.stage]
    try:
        for stage in stages:
            if stage == "list":
                crawl.crawl_list(conn, fetcher, args.since, args.max_list_pages, args.refresh_days)
            elif stage == "overview":
                crawl.crawl_overviews(conn, fetcher, args.limit, args.reparse)
            elif stage == "gallery":
                crawl.crawl_galleries(conn, fetcher, args.min_cash, args.limit, args.reparse)
            elif stage == "categories":
                crawl.crawl_categories(conn, fetcher, args.min_submissions, args.category_pages,
                                       args.limit, args.reparse)
            elif stage == "projects":
                crawl.crawl_projects(conn, fetcher, args.loser_rate, args.min_losers, args.max_losers,
                                     args.top_liked, args.limit, reparse=args.reparse,
                                     min_submissions=args.min_submissions)
    except KeyboardInterrupt:
        fetcher.stop()
        print("\nstopped; everything fetched so far is saved. Re-run the same command to resume.")
    except CrawlHalted as exc:
        print(f"\nhalted: {exc}")
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
        f" | events with categories {q('SELECT COUNT(*) FROM hackathons WHERE categories_done > 0')}"
        f" | pages cached {q('SELECT COUNT(*) FROM pages WHERE status = 200')}"
        f" | failed {q('SELECT COUNT(*) FROM pages WHERE status IS NULL OR status != 200')}"
    )


if __name__ == "__main__":
    main()
