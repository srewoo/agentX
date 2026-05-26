"""Tests for unusual options activity detector."""
from __future__ import annotations

import pytest

from app.services.broker import OptionChainSnapshot, OptionChainStrike, OptionLeg
from app.services.unusual_options_activity import (
    as_signal,
    detect_unusual_activity,
)


def _chain(spot: float, rows: list[dict]) -> OptionChainSnapshot:
    """Build a chain where each row supplies (strike, ce_vol, ce_oi, pe_vol, pe_oi)."""
    strikes = []
    for r in rows:
        strikes.append(OptionChainStrike(
            strike=r["strike"],
            call=OptionLeg(strike=r["strike"], volume=r["ce_vol"], oi=r["ce_oi"], last_price=10.0),
            put=OptionLeg(strike=r["strike"], volume=r["pe_vol"], oi=r["pe_oi"], last_price=10.0),
        ))
    return OptionChainSnapshot(underlying="NIFTY", expiry="2026-05-29", spot=spot, strikes=strikes)


def test_flags_single_outlier_strike():
    """One strike with 10× normal volume should land at the top."""
    rows = []
    for k in range(23800, 24300, 100):
        rows.append({"strike": k, "ce_vol": 1000, "ce_oi": 5000, "pe_vol": 1000, "pe_oi": 5000})
    # Inject an outlier: CE at 24200 has 20k volume + 25k OI
    rows.append({"strike": 24400, "ce_vol": 20000, "ce_oi": 25000, "pe_vol": 1000, "pe_oi": 5000})

    chain = _chain(spot=24000.0, rows=rows)
    flagged = detect_unusual_activity(chain)
    assert len(flagged) >= 1
    top = flagged[0]
    assert top.strike == 24400
    assert top.option_type == "CE"
    assert top.direction_hint == "bullish"   # CE above spot → bullish flow


def test_no_flag_when_chain_is_uniform():
    rows = [
        {"strike": k, "ce_vol": 1000, "ce_oi": 5000, "pe_vol": 1000, "pe_oi": 5000}
        for k in range(23800, 24400, 100)
    ]
    flagged = detect_unusual_activity(_chain(spot=24000, rows=rows))
    assert flagged == []


def test_low_absolute_volume_outlier_is_ignored():
    """An outlier on near-zero baseline shouldn't flag (avoid noise)."""
    # All other strikes have 1 contract → median = 1. A strike with
    # volume=3 = 3× median ratio but tiny absolute — should NOT pass
    # because the absolute ratio threshold compounds with z-score.
    rows = [
        {"strike": k, "ce_vol": 1, "ce_oi": 1, "pe_vol": 1, "pe_oi": 1}
        for k in range(23800, 24300, 100)
    ]
    # Outlier strike with ratio 3× but z-score may be high due to zero MAD.
    rows.append({"strike": 24400, "ce_vol": 3, "ce_oi": 3, "pe_vol": 1, "pe_oi": 1})
    flagged = detect_unusual_activity(_chain(spot=24000, rows=rows))
    # With zero-MAD baseline, robust_z returns 0 → no flag.
    assert flagged == []


def test_directional_hint_for_put_below_spot_is_bearish():
    """Heavy put volume at strike < spot ⇒ bearish hedge / directional bet."""
    rows = []
    for k in range(23800, 24400, 100):
        rows.append({"strike": k, "ce_vol": 1000, "ce_oi": 5000, "pe_vol": 1000, "pe_oi": 5000})
    rows.append({"strike": 23500, "ce_vol": 1000, "ce_oi": 5000, "pe_vol": 30000, "pe_oi": 35000})

    chain = _chain(spot=24000.0, rows=rows)
    flagged = detect_unusual_activity(chain)
    bear_puts = [a for a in flagged if a.option_type == "PE"]
    assert bear_puts and bear_puts[0].direction_hint == "bearish"


def test_as_signal_emits_standard_shape():
    rows = []
    for k in range(23800, 24400, 100):
        rows.append({"strike": k, "ce_vol": 1000, "ce_oi": 5000, "pe_vol": 1000, "pe_oi": 5000})
    rows.append({"strike": 24400, "ce_vol": 20000, "ce_oi": 25000, "pe_vol": 1000, "pe_oi": 5000})

    chain = _chain(spot=24000.0, rows=rows)
    activity = detect_unusual_activity(chain)[0]
    sig = as_signal(activity, "test-id")
    assert sig["id"] == "test-id"
    assert sig["signal_type"] == "unusual_options_activity"
    assert sig["direction"] in ("bullish", "bearish", "neutral")
    assert 1 <= sig["strength"] <= 10
    assert "metadata" in sig and sig["metadata"]["strike"] == 24400


def test_empty_chain_returns_empty():
    chain = OptionChainSnapshot(underlying="X", expiry="x", spot=100, strikes=[])
    assert detect_unusual_activity(chain) == []
