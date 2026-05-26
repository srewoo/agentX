"""Tests for the action_from_score conviction overhaul (#16)."""
from __future__ import annotations

from app.services.recommendation import action_from_score


# ── Base thresholds (no bypass) ──────────────────────────────────────────

def test_neutral_below_threshold_is_hold():
    # Was BUY with old ±0.15; should now be HOLD under new ±0.10 if score
    # is +0.08. Actually +0.08 < new threshold 0.10 → HOLD.
    assert action_from_score(0.08, regime="neutral") == "HOLD"


def test_neutral_at_new_threshold_is_buy():
    """+0.11 is below the OLD ±0.15 (would HOLD) but above the new ±0.10."""
    assert action_from_score(0.11, regime="neutral") == "BUY"


def test_neutral_negative_threshold():
    assert action_from_score(-0.11, regime="neutral") == "SELL"


def test_neutral_borderline_zero_is_hold():
    assert action_from_score(0.0, regime="neutral") == "HOLD"


# ── Regime-adaptive thresholds ───────────────────────────────────────────

def test_trend_up_relaxes_buy_threshold():
    """In trend_up regime, BUY threshold drops to 0.08."""
    assert action_from_score(0.09, regime="trend_up") == "BUY"
    # But SELL threshold stays 0.15 in trend_up.
    assert action_from_score(-0.11, regime="trend_up") == "HOLD"


def test_trend_down_relaxes_sell_threshold():
    assert action_from_score(-0.09, regime="trend_down") == "SELL"
    assert action_from_score(0.11, regime="trend_down") == "HOLD"


def test_risk_off_keeps_thresholds_strict():
    assert action_from_score(0.12, regime="risk_off") == "HOLD"
    assert action_from_score(0.16, regime="risk_off") == "BUY"


# ── High-agreement bypass ────────────────────────────────────────────────

def test_high_agreement_bypass_lowers_threshold():
    """3+ aligned signals + agreement 0.7+ should halve the threshold,
    so a score of 0.06 (which would normally HOLD at 0.10) becomes BUY."""
    out = action_from_score(
        0.06, regime="neutral", factor_agreement=0.8,
        signal_count=3, deterministic_consensus="bullish",
    )
    assert out == "BUY"


def test_bypass_skipped_without_enough_signals():
    """Only 2 signals — bypass doesn't trigger, 0.06 stays HOLD."""
    out = action_from_score(
        0.06, regime="neutral", factor_agreement=0.9,
        signal_count=2, deterministic_consensus="bullish",
    )
    assert out == "HOLD"


def test_bypass_skipped_when_judge_dropped():
    """LLM judge=drop blocks the bypass."""
    out = action_from_score(
        0.06, regime="neutral", factor_agreement=0.8,
        signal_count=4, deterministic_consensus="bullish",
        llm_verdict="drop",
    )
    assert out == "HOLD"


def test_bypass_skipped_with_low_agreement():
    """Even with 5 signals, low factor agreement blocks the bypass."""
    out = action_from_score(
        0.06, regime="neutral", factor_agreement=0.4,
        signal_count=5, deterministic_consensus="bullish",
    )
    assert out == "HOLD"


def test_bypass_works_for_bearish_consensus():
    out = action_from_score(
        -0.06, regime="neutral", factor_agreement=0.75,
        signal_count=3, deterministic_consensus="bearish",
    )
    assert out == "SELL"
