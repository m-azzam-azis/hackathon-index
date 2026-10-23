"""Polite, resumable fetcher.

Every response body is cached gzipped on disk and recorded in `pages`, so a page is
fetched once and can be re-parsed any number of times.

Pacing adapts: every retryable failure (429, 403, 5xx, network error) doubles the gap
between requests (up to MAX_INTERVAL), and each streak of successes eases it back
toward `min_interval`. If several URLs in a row fail even after all retries (offline,
or blocked), the run stops with `CrawlHalted`; re-running resumes where it left off,
and failed URLs are retried because only final statuses (200, 404, ...) count as done.

Thread-safe: with `workers` > 1, several threads call `get` at once. Request starts are
spaced globally by interval / workers, so each worker effectively keeps `interval`, and
one slowdown applies to all of them. Give the fetcher its own connection opened with
check_same_thread=False; all its DB access is serialised by a lock.
"""

import gzip
import hashlib
import random
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

USER_AGENT = "hackathon-index/0.1 (personal research crawler; slow and cached)"
RETRY_STATUSES = {403, 408, 429, 500, 502, 503, 504}  # 403: Devpost may block before it 429s
BACKOFF = [60, 120, 300, 600]
MAX_INTERVAL = 30.0
EASE_AFTER = 50          # successes in a row before easing the interval back down
HALT_AFTER = 3           # URLs in a row that exhausted every retry


class CrawlHalted(RuntimeError):
    pass


class Stopped(RuntimeError):
    """Raised inside workers after stop() so they abandon backoff sleeps promptly."""


class Fetcher:
    def __init__(self, conn: sqlite3.Connection, raw_dir: Path, min_interval: float = 0.5,
                 workers: int = 1):
        self.conn = conn
        self.raw_dir = raw_dir
        self.min_interval = min_interval
        self.interval = min_interval
        self.workers = max(1, workers)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._next_slot = 0.0
        self._ok_streak = 0
        self._dead_streak = 0
        self.requests = 0
        self.retries = 0
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
            timeout=30,
            follow_redirects=True,
        )

    def stop(self) -> None:
        self._stop.set()

    def cached(self, url: str) -> str | None:
        with self._lock:
            row = self.conn.execute("SELECT status, path FROM pages WHERE url=?", (url,)).fetchone()
        if row and row["status"] == 200 and row["path"] and Path(row["path"]).exists():
            return gzip.decompress(Path(row["path"]).read_bytes()).decode("utf-8")
        return None

    def seen(self, url: str) -> bool:
        """True if fetched before with a final (non-retryable) status, e.g. 200 or 404."""
        with self._lock:
            row = self.conn.execute("SELECT status FROM pages WHERE url=?", (url,)).fetchone()
        return bool(row and row["status"] and row["status"] not in RETRY_STATUSES)

    def get(self, url: str, source: str, kind: str, refresh: bool = False) -> str | None:
        if not refresh:
            body = self.cached(url)
            if body is not None:
                return body
            if self.seen(url):
                return None  # e.g. a 404 we already recorded

        for attempt in range(len(BACKOFF) + 1):
            self._pace()
            with self._lock:
                self.requests += 1
            try:
                resp = self.client.get(url)
            except httpx.HTTPError as exc:
                status, error, wait = None, repr(exc), BACKOFF[min(attempt, len(BACKOFF) - 1)]
            else:
                status, error = resp.status_code, None
                if status not in RETRY_STATUSES:
                    self._succeeded()
                    if status == 200:
                        self._record(url, source, kind, status, self._store(url, source, resp.content))
                        return resp.text
                    self._record(url, source, kind, status, None, f"HTTP {status}")
                    return None
                wait = _retry_after(resp) or BACKOFF[min(attempt, len(BACKOFF) - 1)]
                error = f"HTTP {status}"
            self._slow_down()
            if attempt < len(BACKOFF):
                print(f"\n  ! {error} on {url}; sleeping {wait}s, then pacing at {self.interval:.1f}s", flush=True)
                self._sleep(wait)

        self._record(url, source, kind, status, None, error)
        with self._lock:
            self._dead_streak += 1
            dead = self._dead_streak
        if dead >= HALT_AFTER:
            raise CrawlHalted(f"{HALT_AFTER} URLs in a row failed after all retries (last: {error}). "
                              "Offline or blocked? Re-run later to resume.")
        return None

    def _succeeded(self) -> None:
        with self._lock:
            self._dead_streak = 0
            self._ok_streak += 1
            if self._ok_streak >= EASE_AFTER and self.interval > self.min_interval:
                self.interval = max(self.min_interval, self.interval * 0.8)
                self._ok_streak = 0

    def _slow_down(self) -> None:
        with self._lock:
            self._ok_streak = 0
            self.retries += 1
            self.interval = min(MAX_INTERVAL, self.interval * 2)

    def _pace(self) -> None:
        if self._stop.is_set():
            raise Stopped()
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_slot)
            self._next_slot = start + (self.interval + random.uniform(0, 0.3)) / self.workers
        if start > now:
            time.sleep(start - now)

    def _sleep(self, seconds: float) -> None:
        """Backoff sleep in 1s steps so stop() (Ctrl-C) interrupts it."""
        for i in range(int(seconds + 0.999)):
            if self._stop.is_set():
                raise Stopped()
            time.sleep(min(1, seconds - i))

    def _store(self, url: str, source: str, content: bytes) -> str:
        digest = hashlib.sha1(url.encode()).hexdigest()
        path = self.raw_dir / source / digest[:2] / f"{digest}.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(content))
        return str(path)

    def _record(self, url, source, kind, status, path, error=None) -> None:
        with self._lock:
            self._record_locked(url, source, kind, status, path, error)

    def _record_locked(self, url, source, kind, status, path, error) -> None:
        self.conn.execute(
            "INSERT INTO pages (url, source, kind, status, fetched_at, path, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (url) DO UPDATE SET "
            "status=excluded.status, fetched_at=excluded.fetched_at, path=excluded.path, error=excluded.error",
            (url, source, kind, status, datetime.now(timezone.utc).isoformat(), path, error),
        )
        self.conn.commit()


def _retry_after(resp: httpx.Response) -> int | None:
    value = resp.headers.get("Retry-After", "")
    return int(value) if value.isdigit() else None
