"""Tests for the max-pain analysis."""
from __future__ import annotations

import pytest

from app.services.options_max_pain import (
    compute_max_pain,
    compute_max_pain_for_expiry,
    max_pain_directional_bias,
)


def _sym_chain(spot: float, max_pain_strike: float, step: float = 100.0, width: int = 5) -> dict:
    """Generate a symmetric chain centered so the analytic max pain
    matches ``max_pain_strike`` (more puts below it, more calls above)."""
    strikes = []
    for i in range(-width, width + 1):
        K = max_pain_strike + i * step
        # Puts thick below max-pain, calls thick above — both peaking at edge.
        pe_oi = max(0, -i) * 1000 + 500
        ce_oi = max(0, i) * 1000 + 500
        strikes.append({"strike": K, "ce_oi": ce_oi, "pe_oi": pe_oi})
    return {"spot": spot, "expiries": [{"expiry": "2026-05-29", "strikes": strikes}]}


def test_max_pain_picks_centre_of_symmetric_chain():
    chain = _sym_chain(spot=24000.0, max_pain_strike=24200.0)
    results = compute_max_pain(chain)
    assert len(results) == 1
    r = results[0]
    assert r.max_pain_strike == 24200.0
    # spot 24000 < 24200 → distance positive
    assert r.distance_pct == pytest.approx(((24200 - 24000) / 24000) * 100, abs=1e-6)


def test_empty_chain_returns_empty_list():
    assert compute_max_pain({"spot": 100, "expiries": []}) == []


def test_missing_spot_returns_no_result():
    chain = {
        "spot": 0,
        "expiries": [{"expiry": "x", "strikes": [{"strike": 100, "ce_oi": 1, "pe_oi": 1}]}],
    }
    assert compute_max_pain(chain) == []


def test_compute_for_expiry_handles_empty_strikes():
    assert compute_max_pain_for_expiry("x", [], spot=100) is None


def test_directional_bias_when_max_pain_above_spot_is_bullish():
    chain = _sym_chain(spot=24000.0, max_pain_strike=24500.0)
    r = compute_max_pain(chain)[0]
    assert max_pain_directional_bias(r) == "bullish"


def test_directional_bias_when_max_pain_below_spot_is_bearish():
    chain = _sym_chain(spot=24500.0, max_pain_strike=24000.0)
    r = compute_max_pain(chain)[0]
    assert max_pain_directional_bias(r) == "bearish"


def test_directional_bias_near_spot_is_neutral():
    chain = _sym_chain(spot=24000.0, max_pain_strike=24000.0)
    r = compute_max_pain(chain)[0]
    assert max_pain_directional_bias(r) == "neutral"


def test_caps_at_max_expiries():
    # 8 expiries, only first 6 should be processed.
    expiries = []
    for i in range(8):
        expiries.append({
            "expiry": f"E{i}",
            "strikes": [
                {"strike": 100, "ce_oi": 1, "pe_oi": 1},
                {"strike": 110, "ce_oi": 1, "pe_oi": 5},
            ],
        })
    chain = {"spot": 105, "expiries": expiries}
    results = compute_max_pain(chain, max_expiries=6)
    assert len(results) == 6
    assert [r.expiry for r in results] == [f"E{i}" for i in range(6)]
