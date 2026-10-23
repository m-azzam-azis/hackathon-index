"""Polite, resumable fetcher.

Every response body is cached gzipped on disk and recorded in `pages`, so a page is
fetched once and can be re-parsed any number of times. Requests are paced
(`min_interval` + jitter) and back off on 429/5xx, honouring Retry-After.
"""

import gzip
import hashlib
import random
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

USER_AGENT = "hackathon-index/0.1 (personal research crawler; slow and cached)"
RETRY_STATUSES = {429, 500, 502, 503, 504}
BACKOFF = [60, 120, 300, 600]


class Fetcher:
    def __init__(self, conn: sqlite3.Connection, raw_dir: Path, min_interval: float = 1.5):
        self.conn = conn
        self.raw_dir = raw_dir
        self.min_interval = min_interval
        self._last = 0.0
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
            timeout=30,
            follow_redirects=True,
        )

    def cached(self, url: str) -> str | None:
        row = self.conn.execute("SELECT status, path FROM pages WHERE url=?", (url,)).fetchone()
        if row and row["status"] == 200 and row["path"] and Path(row["path"]).exists():
            return gzip.decompress(Path(row["path"]).read_bytes()).decode("utf-8")
        return None

    def seen(self, url: str) -> bool:
        """True if fetched before with a final (non-retryable) status, e.g. 200 or 404."""
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
            try:
                resp = self.client.get(url)
            except httpx.HTTPError as exc:
                status, error, wait = None, repr(exc), BACKOFF[min(attempt, len(BACKOFF) - 1)]
            else:
                status, error = resp.status_code, None
                if status == 200:
                    self._record(url, source, kind, status, self._store(url, source, resp.content))
                    return resp.text
                if status not in RETRY_STATUSES:
                    self._record(url, source, kind, status, None, f"HTTP {status}")
                    return None
                wait = _retry_after(resp) or BACKOFF[min(attempt, len(BACKOFF) - 1)]
                error = f"HTTP {status}"
            if attempt < len(BACKOFF):
                print(f"  ! {error} on {url}; sleeping {wait}s", flush=True)
                time.sleep(wait)

        self._record(url, source, kind, status, None, error)
        return None

    def _pace(self) -> None:
        delay = self.min_interval + random.uniform(0, 0.5) - (time.monotonic() - self._last)
        if delay > 0:
            time.sleep(delay)
        self._last = time.monotonic()

    def _store(self, url: str, source: str, content: bytes) -> str:
        digest = hashlib.sha1(url.encode()).hexdigest()
        path = self.raw_dir / source / digest[:2] / f"{digest}.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(content))
        return str(path)

    def _record(self, url, source, kind, status, path, error=None) -> None:
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
