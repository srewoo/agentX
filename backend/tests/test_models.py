from __future__ import annotations

"""Tests for app.models — Pydantic request/response model validation."""

import pytest
from pydantic import ValidationError

from app.models import (
    AIAnalysisRequest,
    AddWatchlistRequest,
    CreateAlertRequest,
    Signal,
    UpdateSettingsRequest,
)


# ---------------------------------------------------------------------------
# AIAnalysisRequest
# ---------------------------------------------------------------------------

class TestAIAnalysisRequest:

    def test_given_valid_intraday_when_created_then_succeeds(self):
        req = AIAnalysisRequest(timeframe="intraday")
        assert req.timeframe == "intraday"

    def test_given_valid_swing_when_created_then_succeeds(self):
        req = AIAnalysisRequest(timeframe="swing")
        assert req.timeframe == "swing"

    def test_given_valid_long_when_created_then_succeeds(self):
        req = AIAnalysisRequest(timeframe="long")
        assert req.timeframe == "long"

    def test_given_default_when_created_then_is_swing(self):
        req = AIAnalysisRequest()
        assert req.timeframe == "swing"

    def test_given_invalid_timeframe_when_created_then_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            AIAnalysisRequest(timeframe="monthly")
        assert "timeframe" in str(exc_info.value).lower()

    def test_given_empty_timeframe_when_created_then_raises(self):
        with pytest.raises(ValidationError):
            AIAnalysisRequest(timeframe="")


# ---------------------------------------------------------------------------
# UpdateSettingsRequest
# ---------------------------------------------------------------------------

class TestUpdateSettingsRequest:

    def test_given_valid_risk_mode_conservative_when_created_then_succeeds(self):
        req = UpdateSettingsRequest(risk_mode="conservative")
        assert req.risk_mode == "conservative"

    def test_given_valid_risk_mode_balanced_when_created_then_succeeds(self):
        req = UpdateSettingsRequest(risk_mode="balanced")
        assert req.risk_mode == "balanced"

    def test_given_valid_risk_mode_aggressive_when_created_then_succeeds(self):
        req = UpdateSettingsRequest(risk_mode="aggressive")
        assert req.risk_mode == "aggressive"

    def test_given_invalid_risk_mode_when_created_then_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            UpdateSettingsRequest(risk_mode="yolo")
        assert "risk_mode" in str(exc_info.value).lower()

    def test_given_valid_interval_5_when_created_then_succeeds(self):
        req = UpdateSettingsRequest(alert_interval_minutes=5)
        assert req.alert_interval_minutes == 5

    def test_given_valid_interval_1440_when_created_then_succeeds(self):
        req = UpdateSettingsRequest(alert_interval_minutes=1440)
        assert req.alert_interval_minutes == 1440

    def test_given_interval_below_min_when_created_then_raises(self):
        with pytest.raises(ValidationError):
            UpdateSettingsRequest(alert_interval_minutes=4)

    def test_given_interval_above_max_when_created_then_raises(self):
        with pytest.raises(ValidationError):
            UpdateSettingsRequest(alert_interval_minutes=1441)

    def test_given_all_none_when_created_then_succeeds(self):
        req = UpdateSettingsRequest()
        assert req.risk_mode is None
        assert req.alert_interval_minutes is None

    def test_given_valid_llm_provider_when_created_then_succeeds(self):
        req = UpdateSettingsRequest(llm_provider="openai")
        assert req.llm_provider == "openai"

    def test_given_invalid_llm_provider_when_created_then_raises(self):
        with pytest.raises(ValidationError):
            UpdateSettingsRequest(llm_provider="llama")

    def test_given_llm_model_too_long_when_created_then_raises(self):
        with pytest.raises(ValidationError):
            UpdateSettingsRequest(llm_model="a" * 101)


# ---------------------------------------------------------------------------
# AddWatchlistRequest
# ---------------------------------------------------------------------------

class TestAddWatchlistRequest:

    def test_given_valid_request_when_created_then_succeeds(self):
        req = AddWatchlistRequest(symbol="RELIANCE", name="Reliance Industries")
        assert req.symbol == "RELIANCE"
        assert req.name == "Reliance Industries"
        assert req.exchange == "NSE"

    def test_given_bse_exchange_when_created_then_succeeds(self):
        req = AddWatchlistRequest(symbol="TCS", name="Tata Consultancy Services", exchange="BSE")
        assert req.exchange == "BSE"

    def test_given_empty_symbol_when_created_then_raises(self):
        with pytest.raises(ValidationError):
            AddWatchlistRequest(symbol="", name="Test Stock")

    def test_given_empty_name_when_created_then_raises(self):
        with pytest.raises(ValidationError):
            AddWatchlistRequest(symbol="TEST", name="")

    def test_given_too_long_name_when_created_then_raises(self):
        with pytest.raises(ValidationError):
            AddWatchlistRequest(symbol="TEST", name="A" * 101)

    def test_given_too_long_symbol_when_created_then_raises(self):
        with pytest.raises(ValidationError):
            AddWatchlistRequest(symbol="A" * 21, name="Test")

    def test_given_invalid_exchange_when_created_then_raises(self):
        with pytest.raises(ValidationError):
            AddWatchlistRequest(symbol="TEST", name="Test Stock", exchange="NYSE")

    def test_given_default_exchange_when_created_then_nse(self):
        req = AddWatchlistRequest(symbol="INFY", name="Infosys")
        assert req.exchange == "NSE"


# ---------------------------------------------------------------------------
# CreateAlertRequest
# ---------------------------------------------------------------------------

class TestCreateAlertRequest:

    def test_given_valid_above_alert_when_created_then_succeeds(self):
        req = CreateAlertRequest(
            symbol="RELIANCE", target_price=2600.0, condition="above"
        )
        assert req.symbol == "RELIANCE"
        assert req.target_price == 2600.0
        assert req.condition == "above"

    def test_given_valid_below_alert_when_created_then_succeeds(self):
        req = CreateAlertRequest(
            symbol="TCS", target_price=3500.0, condition="below"
        )
        assert req.condition == "below"

    def test_given_pct_change_condition_when_created_then_succeeds(self):
        req = CreateAlertRequest(
            symbol="INFY",
            target_price=1500.0,
            condition="pct_change",
            pct_threshold=5.0,
        )
        assert req.condition == "pct_change"
        assert req.pct_threshold == 5.0

    def test_given_invalid_condition_when_created_then_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            CreateAlertRequest(
                symbol="TEST", target_price=100.0, condition="invalid"
            )
        assert "condition" in str(exc_info.value).lower()

    def test_given_negative_price_when_created_then_raises(self):
        with pytest.raises(ValidationError):
            CreateAlertRequest(
                symbol="TEST", target_price=-100.0, condition="above"
            )

    def test_given_zero_price_when_created_then_raises(self):
        with pytest.raises(ValidationError):
            CreateAlertRequest(
                symbol="TEST", target_price=0.0, condition="above"
            )

    def test_given_empty_symbol_when_created_then_raises(self):
        with pytest.raises(ValidationError):
            CreateAlertRequest(
                symbol="", target_price=100.0, condition="above"
            )

    def test_given_note_when_created_then_preserves_note(self):
        req = CreateAlertRequest(
            symbol="SBIN", target_price=800.0, condition="above",
            note="Buy on breakout above 800",
        )
        assert req.note == "Buy on breakout above 800"

    def test_given_note_too_long_when_created_then_raises(self):
        with pytest.raises(ValidationError):
            CreateAlertRequest(
                symbol="SBIN", target_price=800.0, condition="above",
                note="X" * 501,
            )

    def test_given_pct_threshold_below_min_when_created_then_raises(self):
        with pytest.raises(ValidationError):
            CreateAlertRequest(
                symbol="TEST", target_price=100.0, condition="pct_change",
                pct_threshold=0.05,
            )

    def test_given_pct_threshold_above_max_when_created_then_raises(self):
        with pytest.raises(ValidationError):
            CreateAlertRequest(
                symbol="TEST", target_price=100.0, condition="pct_change",
                pct_threshold=51.0,
            )


# ---------------------------------------------------------------------------
# Signal model
# ---------------------------------------------------------------------------

class TestSignalModel:

    def test_given_valid_signal_when_created_then_succeeds(self, sample_signal):
        sig = Signal(**sample_signal)
        assert sig.symbol == "RELIANCE"
        assert sig.strength == 7

    def test_given_strength_below_1_when_created_then_raises(self):
        with pytest.raises(ValidationError):
            Signal(
                id="test", symbol="TEST", signal_type="test",
                direction="bullish", strength=0, reason="test",
                created_at="2026-01-01T00:00:00Z",
            )

    def test_given_strength_above_10_when_created_then_raises(self):
        with pytest.raises(ValidationError):
            Signal(
                id="test", symbol="TEST", signal_type="test",
                direction="bullish", strength=11, reason="test",
                created_at="2026-01-01T00:00:00Z",
            )
