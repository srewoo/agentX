from __future__ import annotations
"""Tests for app.services.orchestrator — scan cycle, market hours, scheduler lifecycle."""

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

# ─────────────────────────────────────────────
# is_market_open — time-controlled tests
# ─────────────────────────────────────────────

IST = timezone(timedelta(hours=5, minutes=30))


def _ist(hour: int, minute: int = 0, weekday: int = 0) -> datetime:
    """Build a datetime in IST with the given hour/minute, on a weekday (0=Mon)."""
    # Find a Monday in the past to anchor the weekday
    base = datetime(2024, 1, 1, tzinfo=IST)  # Monday 2024-01-01
    days_ahead = weekday - base.weekday()
    if days_ahead < 0:
        days_ahead += 7
    target_date = base.date()
    import datetime as dt_mod
    d = dt_mod.date(2024, 1, 1) + dt_mod.timedelta(days=days_ahead)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=IST)


class TestIsMarketOpen:
    def _run(self, fake_now: datetime) -> bool:
        from app.services import orchestrator as orch_mod
        with patch.object(orch_mod, "datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            # Make replace work on the mock by forwarding to real datetime
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            # Actually call the real function — patch the datetime class inside the module
            import importlib
            # Simpler: patch via freezegun-like approach using datetime.now directly
            pass
        # Direct approach: monkeypatch datetime.now in the module
        from app.services.orchestrator import is_market_open
        with patch("app.services.orchestrator.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            # We need .replace() to work on the returned value
            mock_dt.now.return_value.weekday.return_value = fake_now.weekday()
            mock_dt.now.return_value.replace = fake_now.replace
            return is_market_open()

    @pytest.mark.parametrize("hour,minute,expected", [
        (9, 15,  True),   # exactly open
        (9, 30,  True),   # midday
        (15, 30, True),   # exactly close
        (15, 31, False),  # 1 minute after close
        (9, 14,  False),  # 1 minute before open
        (8, 0,   False),  # early morning
        (18, 0,  False),  # evening
    ])
    def test_weekday_market_hours(self, hour, minute, expected):
        """Test various times on a weekday (Monday)."""
        from app.services.orchestrator import is_market_open
        fake_now = _ist(hour, minute, weekday=0)  # Monday
        with patch("app.services.orchestrator.datetime") as mock_dt_class:
            mock_dt_class.now.return_value = fake_now
            result = is_market_open()
        assert result == expected, f"{hour}:{minute:02d} IST should be {'open' if expected else 'closed'}"

    def test_saturday_is_closed(self):
        from app.services.orchestrator import is_market_open
        fake_now = _ist(12, 0, weekday=5)  # Saturday noon
        with patch("app.services.orchestrator.datetime") as mock_dt_class:
            mock_dt_class.now.return_value = fake_now
            assert is_market_open() is False

    def test_sunday_is_closed(self):
        from app.services.orchestrator import is_market_open
        fake_now = _ist(12, 0, weekday=6)  # Sunday noon
        with patch("app.services.orchestrator.datetime") as mock_dt_class:
            mock_dt_class.now.return_value = fake_now
            assert is_market_open() is False


# ─────────────────────────────────────────────
# SignalOrchestrator — lifecycle tests
# ─────────────────────────────────────────────

class TestSignalOrchestratorLifecycle:
    @pytest.mark.asyncio
    async def test_start_sets_running_true(self):
        from app.services.orchestrator import SignalOrchestrator
        orch = SignalOrchestrator()
        assert orch.is_running() is False

        with patch.object(orch, "_loop", new=AsyncMock()):
            await orch.start()

        assert orch.is_running() is True
        await orch.stop()

    @pytest.mark.asyncio
    async def test_double_start_is_idempotent(self):
        from app.services.orchestrator import SignalOrchestrator
        orch = SignalOrchestrator()
        loop_calls = [0]

        async def fake_loop():
            loop_calls[0] += 1
            await asyncio.sleep(10)

        with patch.object(orch, "_loop", side_effect=fake_loop):
            await orch.start()
            await orch.start()  # second start should no-op

        await orch.stop()
        assert loop_calls[0] == 1  # loop started only once

    @pytest.mark.asyncio
    async def test_stop_sets_running_false(self):
        from app.services.orchestrator import SignalOrchestrator
        orch = SignalOrchestrator()

        async def fake_loop():
            await asyncio.sleep(10)

        with patch.object(orch, "_loop", side_effect=fake_loop):
            await orch.start()

        await orch.stop()
        assert orch.is_running() is False

    @pytest.mark.asyncio
    async def test_is_running_reflects_state(self):
        from app.services.orchestrator import SignalOrchestrator
        orch = SignalOrchestrator()
        assert orch.is_running() is False

        with patch.object(orch, "_loop", new=AsyncMock()):
            await orch.start()
        assert orch.is_running() is True

        await orch.stop()
        assert orch.is_running() is False


# ─────────────────────────────────────────────
# run_scan_cycle — mocked end-to-end
# ─────────────────────────────────────────────

def _make_sample_df() -> pd.DataFrame:
    import numpy as np
    n = 60
    closes = np.linspace(1000, 1100, n)
    dates = pd.bdate_range(end=datetime.now(), periods=n)
    return pd.DataFrame({
        "Open":   closes * 0.999,
        "High":   closes * 1.005,
        "Low":    closes * 0.995,
        "Close":  closes,
        "Volume": [1_000_000] * n,
    }, index=dates)


SAMPLE_TECHNICALS = {
    "rsi": 55.0,
    "adx": 25.0,
    "macd": {"signal": "Bullish", "macd_line": 10.0, "signal_line": 8.0,
             "macd_line_prev": 9.0, "signal_line_prev": 9.0, "histogram": 2.0},
    "current_price": 1100.0,
    "volume_current": 2_000_000,
    "volume_avg_20": 1_000_000,
    "moving_averages": {},
    "bollinger_bands": {},
}

SAMPLE_SR = {
    "pivot": 1050.0,
    "resistance": {"r1": 1090.0, "r2": 1120.0},
    "support": {"s1": 1010.0, "s2": 980.0},
}


class TestRunScanCycle:
    @pytest.mark.asyncio
    async def test_returns_list_of_signals(self, tmp_db_path):
        sample_df = _make_sample_df()

        with patch("app.services.orchestrator.DB_PATH", tmp_db_path), \
             patch("app.services.orchestrator.async_fetch_history", new=AsyncMock(return_value=sample_df)), \
             patch("app.services.orchestrator.compute_technicals", return_value=SAMPLE_TECHNICALS), \
             patch("app.services.orchestrator.compute_support_resistance", return_value=SAMPLE_SR), \
             patch("app.services.orchestrator.pre_screen_stocks", return_value=["RELIANCE"]), \
             patch("app.services.orchestrator.enrich_signal", new=AsyncMock(return_value="LLM summary")), \
             patch("app.services.orchestrator.check_alerts", new=AsyncMock(return_value=[])), \
             patch("app.services.orchestrator.evaluate_signals", new=AsyncMock(return_value={})), \
             patch("app.services.orchestrator._store_signals", new=AsyncMock()), \
             patch("app.services.orchestrator._get_settings", new=AsyncMock(return_value={
                 "risk_mode": "balanced",
                 "signal_types": '["intraday","swing","long_term"]',
                 "alert_interval_minutes": "30",
             })), \
             patch("app.services.orchestrator._get_previous_prices", new=AsyncMock(return_value={})), \
             patch("app.services.orchestrator._store_current_prices", new=AsyncMock()), \
             patch("app.services.orchestrator._get_watchlist_symbols", new=AsyncMock(return_value=[])):

            from app.services.orchestrator import run_scan_cycle
            result = await run_scan_cycle()

        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_llm_enrichment_called_at_most_once(self, tmp_db_path):
        """Regardless of how many symbols scanned, LLM enrich called max 1x."""
        sample_df = _make_sample_df()
        enrich_mock = AsyncMock(return_value="summary")

        with patch("app.services.orchestrator.DB_PATH", tmp_db_path), \
             patch("app.services.orchestrator.async_fetch_history", new=AsyncMock(return_value=sample_df)), \
             patch("app.services.orchestrator.compute_technicals", return_value=SAMPLE_TECHNICALS), \
             patch("app.services.orchestrator.compute_support_resistance", return_value=SAMPLE_SR), \
             patch("app.services.orchestrator.pre_screen_stocks", return_value=["RELIANCE", "TCS", "INFY"]), \
             patch("app.services.orchestrator.enrich_signal", enrich_mock), \
             patch("app.services.orchestrator.check_alerts", new=AsyncMock(return_value=[])), \
             patch("app.services.orchestrator.evaluate_signals", new=AsyncMock(return_value={})), \
             patch("app.services.orchestrator._store_signals", new=AsyncMock()), \
             patch("app.services.orchestrator._get_settings", new=AsyncMock(return_value={
                 "risk_mode": "aggressive",
                 "signal_types": '["intraday","swing","long_term"]',
             })), \
             patch("app.services.orchestrator._get_previous_prices", new=AsyncMock(return_value={})), \
             patch("app.services.orchestrator._store_current_prices", new=AsyncMock()), \
             patch("app.services.orchestrator._get_watchlist_symbols", new=AsyncMock(return_value=[])):

            from app.services.orchestrator import run_scan_cycle
            await run_scan_cycle()

        assert enrich_mock.call_count <= 1

    @pytest.mark.asyncio
    async def test_signals_sorted_by_strength_descending(self, tmp_db_path):
        sample_df = _make_sample_df()

        def make_signals(sym):
            import uuid
            now = datetime.now(timezone.utc).isoformat()
            return [
                {"id": str(uuid.uuid4()), "symbol": sym, "signal_type": "price_spike",
                 "direction": "bullish", "strength": 6, "reason": "r", "risk": "r",
                 "llm_summary": None, "current_price": 1000.0, "metadata": {},
                 "created_at": now, "read": False, "dismissed": False},
            ]

        with patch("app.services.orchestrator.DB_PATH", tmp_db_path), \
             patch("app.services.orchestrator.async_fetch_history", new=AsyncMock(return_value=sample_df)), \
             patch("app.services.orchestrator.compute_technicals", return_value=SAMPLE_TECHNICALS), \
             patch("app.services.orchestrator.compute_support_resistance", return_value=SAMPLE_SR), \
             patch("app.services.orchestrator.pre_screen_stocks", return_value=["RELIANCE"]), \
             patch("app.services.orchestrator.scan_symbol", side_effect=make_signals), \
             patch("app.services.orchestrator.enrich_signal", new=AsyncMock(return_value="")), \
             patch("app.services.orchestrator.check_alerts", new=AsyncMock(return_value=[])), \
             patch("app.services.orchestrator.evaluate_signals", new=AsyncMock(return_value={})), \
             patch("app.services.orchestrator._store_signals", new=AsyncMock()), \
             patch("app.services.orchestrator._get_settings", new=AsyncMock(return_value={
                 "risk_mode": "aggressive",
                 "signal_types": '["intraday","swing","long_term"]',
             })), \
             patch("app.services.orchestrator._get_previous_prices", new=AsyncMock(return_value={})), \
             patch("app.services.orchestrator._store_current_prices", new=AsyncMock()), \
             patch("app.services.orchestrator._get_watchlist_symbols", new=AsyncMock(return_value=[])):

            from app.services.orchestrator import run_scan_cycle
            result = await run_scan_cycle()

        strengths = [s["strength"] for s in result]
        assert strengths == sorted(strengths, reverse=True)
