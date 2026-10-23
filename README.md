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
| `projects` | full project pages | all winners; optionally `--loser-rate` (min `--min-losers`) of non-winners per event, using a fixed random seed |

The filters are loose on purpose, so small events that are real aren't dropped. Deciding which events are junk is a scoring step on the stored data.

The cash estimate is rough. A prize block labelled "$X in cash" counts as cash. Otherwise, amounts are pulled from the prize text, skipping ones followed by "credits", "worth of", and similar, and the estimate is marked `low` confidence. Classifying prizes properly into cash, credits, or swag is a later LLM pass over `prizes.description`.

## Pacing

The crawler makes one request every `--interval` seconds (default 1.5) plus up to 0.5 s of random jitter, over a single connection.

- **Auto retry.** A 429, 403, 5xx, timeout, or network error is retried up to 4 times, waiting 1, 2, 5, then 10 minutes, and honouring `Retry-After`.
- **Adaptive pacing.** Each retryable failure doubles the gap between requests, up to 30 s. Every 50 successes in a row eases it back toward `--interval`.
- **Halt instead of hammering.** If 3 URLs in a row fail even after all retries (offline, or blocked), the run stops cleanly.
- **Resume.** Every page is cached the moment it arrives, and each stage skips work that's already done. Re-running after a crash, Ctrl-C, or halt picks up where it stopped. Failed URLs are tried again, while final answers like 404 are not. Use `--reparse` to re-run parsers over the cached pages.

Do winners first, losers later. By default, `projects` fetches winners only. Every submission's title, tagline, likes, and team already come from the gallery cards. To fetch full pages for a sample of non-winners, run a second pass, for example `crawl projects --loser-rate 0.1 --min-losers 5`. Cached pages are skipped.

Rough volume for 2024 onward with winners only: about 350 listing requests, a few thousand overview pages, then gallery and winner pages, roughly 15k requests in total. That's around 6–7 hours at the default pace.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q

# Pilot: first 30 listing pages, then the 10 most recent qualifying events
python -m hackathon_index crawl list --max-list-pages 8
python -m hackathon_index crawl overview --limit 10
python -m hackathon_index crawl gallery  --limit 10
python -m hackathon_index crawl projects --limit 10
python -m hackathon_index status

# Full backfill (overnight)
caffeinate -i python -m hackathon_index crawl all --since 2024-01-01   # macOS: keep the laptop awake

# Later: full pages for a sample of non-winners
python -m hackathon_index crawl projects --loser-rate 0.1 --min-losers 5
```

Everything lands in `data/index.db` (SQLite). Open it with `sqlite3`, DB Browser for SQLite, or Datasette.

## Tables

- `hackathons`: one row per event. Listing metadata, `registrants`, `submissions_count` (from the gallery), `cash_usd_est`, `cash_confidence`, `judges_count`.
- `prizes`: one row per prize block: title, value, "in cash" flag, winner count, description.
- `judges`: name and title.
- `projects`: title, tagline, likes, built-with, links, `has_repo`, `has_video`, `image_count`, `writeup` (JSON of section → text), `writeup_chars`.
- `submissions`: project × hackathon, with `is_winner` and gallery rank. A project often appears under several hackathons.
- `submission_prizes`: which prize a project won ("Best Use of Neo4j").
- `people` and `project_members`: Devpost profiles, for builder track records later.
