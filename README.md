# hackathon-index

A local database of past hackathons, their prizes, and their submissions (all winners, plus a sample of non-winners). The goal is to find out what actually wins: by series, by sponsor track, and by kind of idea.

Devpost is the first source. The schema is source-agnostic, so Lablab, DoraHacks, or offline events can be added later as new `sources/<platform>` packages, or entered by hand.

## How it works

Collect first, parse in detail later. Every fetched page is saved gzipped under `data/raw/` and recorded in the `pages` table. Re-running a command never fetches the same page twice, and parsers can be improved and re-run offline.

| Stage | Fetches | Which events |
|---|---|---|
| `list` | Devpost's listing JSON, 9 events per request | every ended event; keeps those that closed on or after `--since` |
| `overview` | the event page (prize blocks and judges) | in-window, not invite-only, with any cash prize |
| `gallery` | every gallery page (all submission cards) | winners announced, and estimated cash ≥ `--min-cash` (or a low-confidence estimate whose headline clears it) |
| `projects` | full project pages | all winners, plus `--loser-rate` (min `--min-losers`) of non-winners per event, using a fixed random seed |

The filters are loose on purpose, so small events that are real aren't dropped. Deciding which events are junk is a scoring step on the stored data.

The cash estimate is rough. A prize block labelled "$X in cash" counts as cash. Otherwise, amounts are pulled from the prize text, skipping ones followed by "credits", "worth of", and similar, and the estimate is marked `low` confidence. Classifying prizes properly into cash, credits, or swag is a later LLM pass over `prizes.description`.

## Pacing

The crawler makes one request every `--interval` seconds (default 4) plus up to 1.5 s of random jitter. It backs off for 1, 2, 5, then 10 minutes on 429 or 5xx responses, honouring `Retry-After`. It runs one connection at a time with an honest user agent. Press Ctrl-C at any point and re-run the same command to resume.

Rough volume for 2024 onward: about 1,550 listing requests, a few thousand overview pages, then gallery and project pages for the qualifying events. Expect about 20k requests in total, or one to two nights at the default pace.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q

# Pilot: first 30 listing pages, then the 10 most recent qualifying events
python -m hackathon_index crawl list --max-list-pages 30
python -m hackathon_index crawl overview --limit 10
python -m hackathon_index crawl gallery  --limit 10
python -m hackathon_index crawl projects --limit 10
python -m hackathon_index status

# Full backfill (overnight)
caffeinate -i python -m hackathon_index crawl all --since 2024-01-01   # macOS: keep the laptop awake
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
