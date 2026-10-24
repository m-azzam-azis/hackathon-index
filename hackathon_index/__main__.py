"""CLI.  hackathon-index <command> [options]   (or: python -m hackathon_index ...)

    menu       interactive menu (what `./run.sh` opens with no arguments)
    crawl      run one Devpost stage, or all of them in order
    status     what's collected so far
    score      junk-event scoring (a new run each time; earlier runs are kept)
    label-events  LLM labels per event: real prize value, legitimacy (--sample N to test first)
    snapshot   save a copy of index.db;  snapshots  list them;  restore NAME  go back to one
"""

import argparse
import fcntl
import sys
from pathlib import Path

from . import snapshot
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

    sub.add_parser("menu")
    sub.add_parser("status")
    sub.add_parser("score")
    le = sub.add_parser("label-events")
    le.add_argument("--sample", type=int, help="test on N events; writes a review file, not the database")
    le.add_argument("--compare", help="comma-separated models for --sample (default: two cheap ones)")
    le.add_argument("--model", help="model for a full run (default: anthropic/claude-haiku-5.5)")
    le.add_argument("--workers", type=int, default=16, help="parallel API calls")
    le.add_argument("--max-cost", type=float, default=3.0, help="stop once this many USD are spent")
    le.add_argument("--limit", type=int, help="label at most N events this run (it resumes later)")
    tp = sub.add_parser("tag-projects")
    tp.add_argument("--sample", type=int, help="test on N projects; writes a review file, not the database")
    tp.add_argument("--compare", help="comma-separated models for --sample (default: two cheap ones)")
    tp.add_argument("--model", help="model for a full run (default: anthropic/claude-haiku-5.5)")
    tp.add_argument("--workers", type=int, default=16, help="parallel API calls")
    tp.add_argument("--max-cost", type=float, default=15.0, help="stop once this many USD are spent")
    tp.add_argument("--limit", type=int, help="tag at most N projects this run (it resumes later)")
    tp.add_argument("--grades", default="AB", help="tag projects from events with these grades (default: AB)")
    sn = sub.add_parser("snapshot")
    sn.add_argument("label", nargs="?", default="manual")
    sub.add_parser("snapshots")
    rs = sub.add_parser("restore")
    rs.add_argument("name", help="snapshot file name, as `snapshots` lists it")
    args = ap.parse_args()

    db_path = args.data / "index.db"
    args.data.mkdir(parents=True, exist_ok=True)
    if args.cmd == "menu":
        from . import menu
        return menu.run(args.data)
    if args.cmd == "snapshots":
        for p in snapshot.listing(db_path):
            print(f"{p.name}  {p.stat().st_size / 1e6:.0f} MB")
        return
    if args.cmd == "snapshot":
        snapshot.take(db_path, args.label)
        return
    if args.cmd in ("score", "restore", "crawl", "label-events", "tag-projects"):
        lock = take_lock(args.data)
    if args.cmd == "restore":
        print(f"restored {snapshot.restore(db_path, args.name).name}")
        return
    conn = connect(db_path)
    if args.cmd == "status":
        return status(conn)
    if args.cmd == "score":
        from .score import score_events
        score_events(conn, str(snapshot.take(db_path, "before-score")))
        return
    if args.cmd == "label-events":
        from . import label_events as le
        if args.sample:
            models = args.compare.split(",") if args.compare else ([args.model] if args.model else le.COMPARE_MODELS)
            le.sample(conn, args.data, args.sample, models, args.workers, args.max_cost)
        else:
            snap = snapshot.take(db_path, "before-label-events")
            le.full_run(conn, args.model or le.DEFAULT_MODEL, args.workers, args.max_cost, str(snap), args.limit)
        return
    if args.cmd == "tag-projects":
        from . import tag_projects as tp
        if args.sample:
            models = args.compare.split(",") if args.compare else ([args.model] if args.model else tp.COMPARE_MODELS)
            tp.sample(conn, args.data, args.sample, models, args.workers, args.max_cost, args.grades.upper())
        else:
            snap = snapshot.take(db_path, "before-tag-projects")
            tp.full_run(conn, args.model or tp.DEFAULT_MODEL, args.workers, args.max_cost, str(snap), args.limit,
                        args.grades.upper())
        return

    if args.reparse:
        snapshot.take(db_path, f"before-reparse-{args.stage}")
    # The fetcher gets its own connection: worker threads share it under the fetcher's lock.
    fetcher = Fetcher(connect(db_path, threads=True), args.data / "raw",
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


def take_lock(data: Path):
    lock = open(data / ".crawl.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("another crawl or step is already running on this data folder; wait for it to finish")
    return lock


def status(conn) -> None:
    n = lambda sql: f"{conn.execute(sql).fetchone()[0]:,}"
    print(f"""
  Events    {n("SELECT COUNT(*) FROM hackathons")} listed · {n("SELECT COUNT(*) FROM hackathons WHERE cash_usd_est IS NOT NULL")} prize pages · \
{n("SELECT COUNT(*) FROM hackathons WHERE submissions_count > 0")} galleries · \
{n("SELECT COUNT(*) FROM hackathons WHERE categories_done > 0")} with categories
  Projects  {n("SELECT COUNT(*) FROM projects")} projects · {n("SELECT COUNT(*) FROM submissions WHERE is_winner = 1")} winners · \
{n("SELECT COUNT(*) FROM projects WHERE detail_fetched = 1")} full pages
  Pages     {n("SELECT COUNT(*) FROM pages WHERE status = 200")} saved · \
{n("SELECT COUNT(*) FROM pages WHERE status IS NULL OR status != 200")} failed""")
    tiers = dict(conn.execute("SELECT tier, COUNT(*) FROM event_scores_latest GROUP BY tier").fetchall())
    if tiers:
        print("  Scoring   " + " · ".join(f"{tiers.get(t, 0):,} {t}" for t in ("ok", "watch", "junk")))
    print()


if __name__ == "__main__":
    main()
