"""Tests for the NSE delivery-session anti-bot hardening:
rate-limit throttle, deeper warm-up, and proactive re-warm."""
from __future__ import annotations

import time

import pytest

from app.services import data_fetcher as df


class _FakeSession:
    def __init__(self):
        self.gets: list[str] = []

    def get(self, url, **kw):
        self.gets.append(url)
        return None


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    # Make warm-up instant — we assert on calls, not on real sleeps.
    monkeypatch.setattr(df.time, "sleep", lambda *_a, **_k: None)
    df._nse_session_warmed_at = 0.0
    df._nse_last_request_at = 0.0
    yield
    df._nse_session_warmed_at = 0.0
    df._nse_last_request_at = 0.0


def test_throttle_enforces_min_interval(monkeypatch):
    # Use the real sleep here but a tiny interval so the test stays fast.
    sleeps: list[float] = []
    monkeypatch.setattr(df.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(df, "_NSE_MIN_INTERVAL", 0.5)

    df._nse_last_request_at = 0.0
    monkeypatch.setattr(df.time, "monotonic", lambda: 100.0)
    df._throttle_nse()  # first call: last_request was "long ago" → no real wait
    # Second call immediately after → must wait ~0.5s.
    df._throttle_nse()
    assert sleeps, "expected a throttle sleep on the back-to-back call"
    assert sleeps[-1] >= 0.5


def test_warm_session_hits_homepage_and_quote_page():
    sess = _FakeSession()
    df._warm_nse_session(sess, "RELIANCE")
    assert any(u == df._NSE_HOMEPAGE for u in sess.gets)
    assert any("get-quotes/equity?symbol=RELIANCE" in u for u in sess.gets)
    # Warm timestamp recorded so the proactive guard knows it's fresh.
    assert df._nse_session_warmed_at > 0.0


def test_warm_session_homepage_only_without_symbol():
    sess = _FakeSession()
    df._warm_nse_session(sess)
    assert sess.gets == [df._NSE_HOMEPAGE]


def test_ensure_warm_skips_when_fresh(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(df, "_warm_nse_session", lambda s, sym=None: calls.__setitem__("n", calls["n"] + 1))
    # Freshly warmed → no re-warm.
    df._nse_session_warmed_at = df.time.monotonic()
    df._ensure_nse_warm(_FakeSession(), "TCS")
    assert calls["n"] == 0


def test_ensure_warm_triggers_when_stale(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(df, "_warm_nse_session", lambda s, sym=None: calls.__setitem__("n", calls["n"] + 1))
    # Last warmed older than the TTL → stale → warm once. Anchored to the
    # current monotonic clock so the test doesn't depend on its absolute origin.
    df._nse_session_warmed_at = df.time.monotonic() - df._NSE_WARM_TTL - 1.0
    df._ensure_nse_warm(_FakeSession(), "TCS")
    assert calls["n"] == 1
