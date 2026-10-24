# hackathon-index

A local database of past hackathons, their prizes, and their submissions (all winners, plus a sample of non-winners). The goal is to find out what actually wins: by series, by sponsor track, and by kind of idea.

Devpost is the first source. The schema is source-agnostic, so Lablab, DoraHacks, or offline events can be added later as new `sources/<platform>` packages, or entered by hand.

## How it works

Collect first, parse in detail later. Every fetched page is saved gzipped under `data/raw/` and recorded in the `pages` table. Re-running a command never fetches the same page twice, and parsers can be improved and re-run offline.

| Stage | Fetches | Which events |
|---|---|---|
| `list` | Devpost's listing JSON, 40 events per request | every ended event; keeps those that closed on or after `--since` |
| `overview` | the event page (prize blocks and judges) | in-window, not invite-only, with any cash prize |
| `gallery` | every gallery page (all submission cards) | winners announced, and estimated cash ≥ `--min-cash` (or a low-confidence estimate whose headline clears it) |
| `categories` | per-prize filtered galleries (`/submissions/search?prize_filter…`), up to `--category-pages` pages each | events with ≥ `--min-submissions` submissions whose gallery has a prize filter; runs with the non-winner pass |
| `projects` | full project pages | all winners; optionally `--loser-rate` of non-winners per event (events with ≥ `--min-submissions`), clamped to `--min-losers`..`--max-losers` (default cap 40) |

The non-winner sample is picked in priority order: the `--top-liked` most-liked (near-miss candidates), then round-robin across categories so every category is represented, then a seeded random fill. It's deterministic: the same event always gives the same picks, new events don't change old ones, and raising the cap only adds projects. The ordering uses project URLs, so even a rebuilt database picks the same ones.

The filters are loose on purpose, so small events that are real aren't dropped. Deciding which events are junk is a scoring step on the stored data.

The cash estimate is rough. A prize block labelled "$X in cash" counts as cash. Otherwise, amounts are pulled from the prize text, skipping ones followed by "credits", "worth of", and similar, and the estimate is marked `low` confidence. Classifying prizes properly into cash, credits, or swag is a later LLM pass over `prizes.description`.

## Pacing

The crawler starts fast and only slows down when Devpost pushes back. By default it runs `--workers 3` in parallel, each making one request every `--interval` seconds (default 0.5) plus up to 0.3 s of random jitter. That's roughly 3x the single-worker speed. Use `--workers 1` for the old sequential behaviour. Threads only fetch and parse, and all database writes happen on the main thread. A lock file stops two crawls running on the same data folder at once.

- **Auto retry.** A 429, 403, 5xx, timeout, or network error is retried up to 4 times, waiting 1, 2, 5, then 10 minutes, and honouring `Retry-After`.
- **Adaptive pacing.** Each retryable failure doubles the gap between requests, up to 30 s. The slowdown is shared, so all workers slow down together. Every 50 successes in a row eases it back toward `--interval`.
- **Halt instead of hammering.** If 3 URLs in a row fail even after all retries (offline, or blocked), the run stops cleanly.
- **Resume.** Every page is cached the moment it arrives, and each stage skips work that's already done. Re-running after a crash, Ctrl-C, or halt picks up where it stopped. Failed URLs are tried again, while final answers like 404 are not. Use `--reparse` to re-run parsers over the cached pages.

Do winners first, losers later. By default, `projects` fetches winners only. Every submission's title, tagline, likes, and team already come from the gallery cards. To fetch full pages for a sample of non-winners, run a second pass, for example `crawl projects --loser-rate 0.1 --min-losers 5`. The cap matters for huge events: a 600-submission event gets 40, not 60. Of those 40, the 10 most-liked are near-miss candidates and the rest are random, so the sample isn't only popular projects. Cached pages are skipped.

Rough volume for 2024 onward with winners only: about 350 listing requests, a few thousand overview pages, then gallery and winner pages, roughly 15k requests in total. That's around 3 hours if Devpost never pushes back, longer if it does.

## Run

```bash
./run.sh            # pull the latest code, set up .venv if needed, crawl every stage (resumes)
./run.sh status     # what's collected so far
./run.sh --limit 10 # any crawl option passes through, e.g. a pilot run
```

On macOS it keeps the laptop awake while running. Each stage prints one live line:

```
[gallery] 112/340 (33%) · eta 41m10s · 1.4 req/s · pace 0.5s · retries 0 · AWS AI Agent Global Hackathon: 610 submissions
```

If `pace` climbs above 0.5s, Devpost is pushing back and the crawler has slowed itself down.

Later, to get full pages for a sample of non-winners, balanced across categories:

```bash
./run.sh --loser-rate 0.15 --min-losers 5   # also runs the category pass first
```

Going further back, or keeping it up to date:

```bash
./run.sh --since 2022-01-01   # add older events; the listing is cached, so old pages aren't re-fetched
./run.sh                      # monthly: re-fetches listing pages with events from the last 60 days
                              # (--refresh-days), then crawls new events and newly announced winners
```

## Cleaning steps (offline, nothing deleted)

Every step after the crawl only adds. It writes its results to its own table under a new run id, never edits the crawl tables or earlier runs, and saves a full copy of `index.db` to `data/snapshots/` first. `crawl ... --reparse` also snapshots first.

```bash
./run.sh score              # junk-event scoring, a new run each time
./run.sh snapshots          # list saved copies
./run.sh restore NAME       # go back to one (the current state is snapshotted first)
./run.sh snapshot my-label  # save a copy by hand
```

Junk scoring (`event_scores`, newest run in the `event_scores_latest` view) adds weighted flags per event and labels it `ok` (score ≤ 1), `watch` (2–3) or `junk` (≥ 4). Flags: `no_cash` (only swag, gift cards or credits found, +4), `low_cash` (under $1k, +2), `tiny_field` (under 10 submissions, +3), `small_field` (10–19, +1), `few_registrants` (under 30, +2), `prize_per_entry_absurd` (over $2,500 per submission with under 20 entries, +2), `no_judges`, `one_off_organizer`, `weak_winner_writeups`, `winners_without_code` (+1 each). Trust signals subtract: `managed_by_devpost` (−2), `repeat_organizer` with 5+ events (−1). `reason` holds the heaviest flag. Nothing is excluded; analysis queries filter on the tier.

Each snapshot is a full copy (about 270 MB today), so delete old ones from `data/snapshots/` by hand when space matters.

Manual equivalent: `python -m hackathon_index crawl {list,overview,gallery,projects,all} [options]`, then `pytest -q` for the tests.

Everything lands in `data/index.db` (SQLite). Open it with `sqlite3`, DB Browser for SQLite, or Datasette.

## Tables

- `hackathons`: one row per event. Listing metadata, `registrants`, `submissions_count` (from the gallery), `cash_usd_est`, `cash_confidence`, `judges_count`.
- `prizes`: one row per prize block: title, value, "in cash" flag, winner count, description.
- `judges`: name and title.
- `projects`: title, tagline, likes, built-with, links, `has_repo`, `has_video`, `video_urls` (YouTube/Vimeo watch links), `image_count`, `writeup` (JSON of section → text), `writeup_chars`.
- `submissions`: project × hackathon, with `is_winner` and gallery rank. A project often appears under several hackathons.
- `submission_prizes`: which prize a project won ("Best Use of Neo4j").
- `people` and `project_members`: Devpost profiles, for builder track records later.
- `enrich_runs` and `event_scores`: one row per cleaning run, and the junk score of each event in each run.
