"""Tests for the per-source negative cache."""
from __future__ import annotations

import time

import pytest

from app.services import source_health


@pytest.fixture(autouse=True)
def _clean():
    source_health.reset()
    yield
    source_health.reset()


def test_unknown_source_is_not_down():
    assert source_health.is_down("nse") is False


def test_mark_down_makes_source_down_until_cooldown():
    source_health.mark_down("nse", cooldown=100)
    assert source_health.is_down("nse") is True


def test_cooldown_expiry_clears_down(monkeypatch):
    source_health.mark_down("yfinance", cooldown=10)
    # Jump past the cooldown window.
    real = time.time()
    monkeypatch.setattr(time, "time", lambda: real + 11)
    assert source_health.is_down("yfinance") is False


def test_mark_up_clears_immediately():
    source_health.mark_down("upstox", cooldown=300)
    source_health.mark_up("upstox")
    assert source_health.is_down("upstox") is False
