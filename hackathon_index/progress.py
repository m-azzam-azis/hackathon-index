"""One live status line per stage: count, ETA, request rate, current pacing, retries."""

import sys
import time


def _dur(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


class Progress:
    def __init__(self, stage: str, total: int, fetcher=None, every: int = 25):
        self.stage, self.total, self.fetcher, self.every = stage, total, fetcher, every
        self.i = 0
        self.start = time.monotonic()
        if fetcher is not None and not hasattr(fetcher, "requests"):
            self.fetcher = None  # test stubs
        self.req0 = self.fetcher.requests if self.fetcher else 0
        self.tty = sys.stdout.isatty()
        print(f"[{stage}] {total} to go", flush=True)

    def step(self, label: str = "") -> None:
        self.i += 1
        if self.tty or self.i % self.every == 0 or self.i == self.total:
            end = "" if self.tty else "\n"
            print(("\r" if self.tty else "") + self.line(label)[:160].ljust(160 if self.tty else 0),
                  end=end, flush=True)

    def line(self, label: str = "") -> str:
        elapsed = time.monotonic() - self.start
        pct = f"{100 * self.i / self.total:.0f}%" if self.total else "?"
        eta = _dur(elapsed / self.i * (self.total - self.i)) if self.i and self.total else "?"
        parts = [f"[{self.stage}] {self.i}/{self.total} ({pct})", f"eta {eta}"]
        if self.fetcher:
            reqs = self.fetcher.requests - self.req0
            parts += [f"{reqs / elapsed if elapsed else 0:.1f} req/s",
                      f"pace {self.fetcher.interval:.1f}s", f"retries {self.fetcher.retries}"]
        if label:
            parts.append(label)
        return " · ".join(parts)

    def done(self, summary: str = "") -> None:
        if self.tty and self.i:
            print()
        print(f"[{self.stage}] done in {_dur(time.monotonic() - self.start)}" +
              (f": {summary}" if summary else ""), flush=True)
