from __future__ import annotations
"""A1 + A2 — autonomous gating state machine.

Core properties:
  * a strong combo only becomes PROMOTED after PROMOTE_AFTER consecutive
    significant rounds (hysteresis — no single-round promotion),
  * a PROMOTED combo is demoted after DEMOTE_AFTER sustained failures,
  * a clearly-losing well-sampled combo is MUTED,
  * derived sets are what get_active_gating returns,
  * seeding from constants starts where the human left off.
"""
import os
import sqlite3
import tempfile

import pytest

from app.services import gating_state as gs
from app.services.multiple_testing import Candidate


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    os.unlink(path)


def _strong():
    return [Candidate("macd_divergence|bullish", wins=144, n=200)]


@pytest.mark.asyncio
async def test_promotion_requires_consecutive_passes(db_path):
    # Two strong rounds: still a candidate (PROMOTE_AFTER == 3).
    for _ in range(gs.PROMOTE_AFTER - 1):
        await gs.update_gating_state(_strong(), db_path=db_path)
    active = await gs.get_active_gating(db_path=db_path)
    assert "macd_divergence|bullish" not in active["promoted"]
    # The PROMOTE_AFTER-th consecutive pass promotes it.
    await gs.update_gating_state(_strong(), db_path=db_path)
    active = await gs.get_active_gating(db_path=db_path)
    assert "macd_divergence|bullish" in active["promoted"]


@pytest.mark.asyncio
async def test_promoted_combo_demoted_after_sustained_failure(db_path):
    for _ in range(gs.PROMOTE_AFTER):
        await gs.update_gating_state(_strong(), db_path=db_path)
    assert "macd_divergence|bullish" in (await gs.get_active_gating(db_path=db_path))["promoted"]
    # Now it stops being significant (coin-flip) for DEMOTE_AFTER rounds.
    weak = [Candidate("macd_divergence|bullish", wins=100, n=200)]
    for _ in range(gs.DEMOTE_AFTER):
        await gs.update_gating_state(weak, db_path=db_path)
    assert "macd_divergence|bullish" not in (await gs.get_active_gating(db_path=db_path))["promoted"]


@pytest.mark.asyncio
async def test_clear_loser_is_muted(db_path):
    losing = [Candidate("evening_star|bullish", wins=60, n=200)]  # 30% WR, n large
    await gs.update_gating_state(losing, db_path=db_path)
    active = await gs.get_active_gating(db_path=db_path)
    assert "evening_star|bullish" in active["muted"]


@pytest.mark.asyncio
async def test_noise_stays_candidate_not_promoted(db_path):
    noise = [Candidate("rsi_extreme|bearish", wins=51, n=100)]
    for _ in range(5):
        await gs.update_gating_state(noise, db_path=db_path)
    active = await gs.get_active_gating(db_path=db_path)
    assert "rsi_extreme|bearish" not in active["promoted"]


@pytest.mark.asyncio
async def test_transitions_reported(db_path):
    for i in range(gs.PROMOTE_AFTER):
        res = await gs.update_gating_state(_strong(), db_path=db_path)
    # The final round should report the candidate->promoted transition.
    assert any(t["to"] == "promoted" for t in res["transitions"])


@pytest.mark.asyncio
async def test_seed_from_constants_then_visible(db_path):
    n = await gs.seed_from_constants(
        promoted=[("double_top", "bearish")],
        muted=[("gap_up", "bullish")],
        blocked=["ITC", "SBIN"],
        db_path=db_path,
    )
    assert n == 4
    active = await gs.get_active_gating(db_path=db_path)
    assert "double_top|bearish" in active["promoted"]
    assert "gap_up|bullish" in active["muted"]
    assert "ITC" in active["blocked"] and "SBIN" in active["blocked"]
    # Idempotent: re-seeding inserts nothing new.
    assert await gs.seed_from_constants([("double_top", "bearish")], [], [], db_path=db_path) == 0


@pytest.mark.asyncio
async def test_overlay_seed_and_predicates(db_path):
    await gs.seed_from_constants(
        promoted=[("double_top", "bearish")], muted=[("gap_up", "bullish")],
        blocked=["ITC"], db_path=db_path)
    loaded = await gs.seed_overlay(db_path=db_path)
    assert loaded == 3
    assert gs.overlay_is_promoted("double_top", "bearish") is True
    assert gs.overlay_is_promoted("rsi_extreme", "bullish") is False
    assert gs.overlay_is_muted("gap_up", "bullish") is True
    assert gs.overlay_is_blocked("ITC") is True
    assert gs.overlay_is_blocked("RELIANCE") is False


def test_overlay_returns_none_when_empty():
    gs.set_overlay({"promoted": set(), "muted": set(), "blocked": set()})
    assert gs.overlay_is_promoted("x", "bullish") is None
    assert gs.overlay_is_blocked("x") is None
