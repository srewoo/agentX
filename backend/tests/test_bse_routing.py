"""BSE routing smoke tests.

Exercises the exchange parameter end-to-end through the data fetcher
candidate-order logic and the fundamentals symbol resolver. No network —
the yfinance call is mocked.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from app.services.data_fetcher import _yfinance_fetch_sync
from app.services.fundamentals import _resolve_yf_symbol


def test_resolve_yf_symbol_defaults_to_ns():
    assert _resolve_yf_symbol("RELIANCE") == "RELIANCE.NS"
    assert _resolve_yf_symbol("RELIANCE", "NSE") == "RELIANCE.NS"


def test_resolve_yf_symbol_bse_uses_bo_suffix():
    assert _resolve_yf_symbol("SHANTIGEAR", "BSE") == "SHANTIGEAR.BO"
    # Case-insensitive
    assert _resolve_yf_symbol("SHANTIGEAR", "bse") == "SHANTIGEAR.BO"


def test_resolve_yf_symbol_preserves_explicit_suffix():
    # Symbols already carrying a suffix are not re-decorated.
    assert _resolve_yf_symbol("RELIANCE.NS", "BSE") == "RELIANCE.NS"
    assert _resolve_yf_symbol("SHANTIGEAR.BO", "NSE") == "SHANTIGEAR.BO"
    assert _resolve_yf_symbol("^NSEI", "BSE") == "^NSEI"


def test_yfinance_fetch_sync_bse_tries_bo_first():
    """When exchange=BSE we must try .BO before .NS — BSE-only listings
    don't exist on .NS and the reverse order would always fail-then-retry."""
    calls: list[str] = []

    def fake_ticker(yf_sym):
        calls.append(yf_sym)
        mock = MagicMock()
        # Return a non-empty frame only on the second attempt to verify the
        # function does iterate when the first candidate is empty.
        if len(calls) == 1:
            mock.history.return_value = pd.DataFrame()
        else:
            mock.history.return_value = pd.DataFrame({"Close": [100.0]})
        return mock

    with patch("app.services.data_fetcher.yf.Ticker", side_effect=fake_ticker):
        _yfinance_fetch_sync("SHANTIGEAR", period="5d", interval="1d", exchange="BSE")

    assert calls[0] == "SHANTIGEAR.BO", f"BSE should try .BO first, got {calls}"
    # If the first attempt returned empty, the implementation may fall through
    # to .NS — that's fine, we only care about ordering.


def test_yfinance_fetch_sync_nse_default_tries_ns_first():
    calls: list[str] = []

    def fake_ticker(yf_sym):
        calls.append(yf_sym)
        mock = MagicMock()
        mock.history.return_value = pd.DataFrame({"Close": [100.0]})
        return mock

    with patch("app.services.data_fetcher.yf.Ticker", side_effect=fake_ticker):
        _yfinance_fetch_sync("RELIANCE", period="5d", interval="1d")

    assert calls[0] == "RELIANCE.NS"


def test_yfinance_fetch_sync_passes_through_indices():
    """Index symbols (^NSEI, ^BSESN) must not be decorated regardless of exchange."""
    calls: list[str] = []

    def fake_ticker(yf_sym):
        calls.append(yf_sym)
        mock = MagicMock()
        mock.history.return_value = pd.DataFrame({"Close": [100.0]})
        return mock

    with patch("app.services.data_fetcher.yf.Ticker", side_effect=fake_ticker):
        _yfinance_fetch_sync("^NSEI", period="5d", interval="1d", exchange="BSE")

    assert calls == ["^NSEI"]
