"""Retry, adaptive pacing, halting, and resume, against a fake transport (no network, no sleeping)."""

import httpx
import pytest

from hackathon_index import fetcher as fetcher_mod
from hackathon_index.db import connect
from hackathon_index.fetcher import CrawlHalted, Fetcher


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(fetcher_mod.time, "sleep", lambda s: None)


def make(tmp_path, handler):
    f = Fetcher(connect(tmp_path / "index.db"), tmp_path / "raw", min_interval=1.5)
    f.client = httpx.Client(transport=httpx.MockTransport(handler))
    return f


def test_retries_then_succeeds_and_slows_down(tmp_path):
    replies = iter([429, 503, 200])
    f = make(tmp_path, lambda req: httpx.Response(next(replies), text="ok"))
    assert f.get("https://x.devpost.com/", "devpost", "overview") == "ok"
    assert f.interval == 6.0  # doubled twice


def test_eases_back_after_success_streak(tmp_path):
    f = make(tmp_path, lambda req: httpx.Response(200, text="ok"))
    f.interval = 6.0
    for i in range(fetcher_mod.EASE_AFTER):
        f.get(f"https://x.devpost.com/{i}", "devpost", "gallery")
    assert f.interval == pytest.approx(4.8)


def test_cached_pages_are_not_refetched(tmp_path):
    calls = []
    f = make(tmp_path, lambda req: (calls.append(1), httpx.Response(200, text="ok"))[1])
    f.get("https://x.devpost.com/", "devpost", "overview")
    f.get("https://x.devpost.com/", "devpost", "overview")
    assert len(calls) == 1


def test_404_is_final_but_failures_are_retried_next_run(tmp_path):
    f = make(tmp_path, lambda req: httpx.Response(404))
    assert f.get("https://x.devpost.com/gone", "devpost", "project") is None
    assert f.seen("https://x.devpost.com/gone")

    f2 = make(tmp_path, lambda req: httpx.Response(503))
    assert f2.get("https://x.devpost.com/flaky", "devpost", "project") is None
    assert not f2.seen("https://x.devpost.com/flaky")  # will be fetched again on re-run


def test_halts_when_many_urls_fail_in_a_row(tmp_path):
    f = make(tmp_path, lambda req: httpx.Response(403))
    f.get("https://x.devpost.com/1", "devpost", "project")
    f.get("https://x.devpost.com/2", "devpost", "project")
    with pytest.raises(CrawlHalted):
        f.get("https://x.devpost.com/3", "devpost", "project")


def test_network_errors_are_retried(tmp_path):
    attempts = iter([True, False])

    def handler(req):
        if next(attempts):
            raise httpx.ConnectError("wifi dropped")
        return httpx.Response(200, text="back")

    f = make(tmp_path, handler)
    assert f.get("https://x.devpost.com/", "devpost", "overview") == "back"
