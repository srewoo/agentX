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
    # Hand-curated promotions are now seeded as CANDIDATES — they must EARN
    # promotion from forward data, not inherit it. Mutes/blocks still inherit.
    assert "double_top|bearish" in active["candidate"]
    assert "double_top|bearish" not in active["promoted"]
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
    # double_top seeds as a candidate now, so only the mute + block load into
    # the active overlay (candidates are not promoted/muted/blocked).
    assert loaded == 2
    # An active overlay (mute present) reports a seeded promotion-candidate as
    # NOT promoted — it must earn promotion before is_promoted() boosts it.
    assert gs.overlay_is_promoted("double_top", "bearish") is False
    assert gs.overlay_is_promoted("rsi_extreme", "bullish") is False
    assert gs.overlay_is_muted("gap_up", "bullish") is True
    assert gs.overlay_is_blocked("ITC") is True
    assert gs.overlay_is_blocked("RELIANCE") is False


def test_overlay_returns_none_when_empty():
    gs.set_overlay({"promoted": set(), "muted": set(), "blocked": set()})
    assert gs.overlay_is_promoted("x", "bullish") is None
    assert gs.overlay_is_blocked("x") is None


# ── 1.3 — per-signal kill criteria & irreversible demotion ──
@pytest.mark.asyncio
async def test_combo_killed_on_weak_live_wilson_lb(db_path):
    # 20/50 live = 40% observed; Wilson-LB well under the 0.40 floor → KILLED.
    cand = [Candidate("gap_down|bearish", wins=100, n=200, live_wins=20, live_n=50)]
    await gs.update_gating_state(cand, db_path=db_path)
    active = await gs.get_active_gating(db_path=db_path)
    assert "gap_down|bearish" in active["killed"]


@pytest.mark.asyncio
async def test_killed_is_terminal(db_path):
    kill = [Candidate("gap_down|bearish", wins=100, n=200, live_wins=15, live_n=50)]
    await gs.update_gating_state(kill, db_path=db_path)
    # Even a string of strong backtest rounds cannot revive a killed combo.
    strong = [Candidate("gap_down|bearish", wins=180, n=200)]
    for _ in range(gs.PROMOTE_AFTER + 2):
        await gs.update_gating_state(strong, db_path=db_path)
    active = await gs.get_active_gating(db_path=db_path)
    assert "gap_down|bearish" in active["killed"]
    assert "gap_down|bearish" not in active["promoted"]


@pytest.mark.asyncio
async def test_demotion_irreversible_without_forward_evidence(db_path):
    key = "macd_divergence|bullish"
    strong = [Candidate(key, wins=144, n=200)]
    for _ in range(gs.PROMOTE_AFTER):
        await gs.update_gating_state(strong, db_path=db_path)
    assert key in (await gs.get_active_gating(db_path=db_path))["promoted"]
    # Sustained failure demotes it.
    weak = [Candidate(key, wins=100, n=200)]
    for _ in range(gs.DEMOTE_AFTER):
        await gs.update_gating_state(weak, db_path=db_path)
    assert key not in (await gs.get_active_gating(db_path=db_path))["promoted"]
    # Backtest passes alone (no live evidence) can no longer re-promote it.
    for _ in range(gs.PROMOTE_AFTER + 2):
        await gs.update_gating_state(strong, db_path=db_path)
    assert key not in (await gs.get_active_gating(db_path=db_path))["promoted"]


@pytest.mark.asyncio
async def test_demoted_combo_repromotes_with_fresh_forward_evidence(db_path):
    key = "macd_divergence|bullish"
    strong = [Candidate(key, wins=144, n=200)]
    for _ in range(gs.PROMOTE_AFTER):
        await gs.update_gating_state(strong, db_path=db_path)
    weak = [Candidate(key, wins=100, n=200)]
    for _ in range(gs.DEMOTE_AFTER):
        await gs.update_gating_state(weak, db_path=db_path)
    # Now backed by a strong FORWARD record (40/50 = 80%, Wilson-LB ≥ 0.50).
    revive = [Candidate(key, wins=144, n=200, live_wins=40, live_n=50)]
    for _ in range(gs.PROMOTE_AFTER):
        await gs.update_gating_state(revive, db_path=db_path)
    assert key in (await gs.get_active_gating(db_path=db_path))["promoted"]


def test_killed_folds_into_muted_overlay():
    gs.set_overlay({"promoted": set(), "muted": set(), "blocked": set(),
                    "killed": {"gap_down|bearish"}})
    assert gs.overlay_is_muted("gap_down", "bearish") is True


# ── A5 — human veto on transitions ──
@pytest.mark.asyncio
async def test_veto_mode_holds_promotion_as_pending(db_path):
    strong = [Candidate("macd_divergence|bullish", wins=144, n=200)]
    for _ in range(gs.PROMOTE_AFTER):
        await gs.update_gating_state(strong, veto_mode=True, db_path=db_path)
    # Live state must NOT have promoted; a pending proposal exists instead.
    active = await gs.get_active_gating(db_path=db_path)
    assert "macd_divergence|bullish" not in active["promoted"]
    pending = await gs.list_pending(db_path=db_path)
    assert any(p["key"] == "macd_divergence|bullish" and p["to_state"] == "promoted"
               for p in pending)


@pytest.mark.asyncio
async def test_resolve_pending_approve_applies(db_path):
    strong = [Candidate("macd_divergence|bullish", wins=144, n=200)]
    for _ in range(gs.PROMOTE_AFTER):
        await gs.update_gating_state(strong, veto_mode=True, db_path=db_path)
    res = await gs.resolve_pending("macd_divergence|bullish", approve=True, db_path=db_path)
    assert res["status"] == "approved"
    active = await gs.get_active_gating(db_path=db_path)
    assert "macd_divergence|bullish" in active["promoted"]
    assert await gs.list_pending(db_path=db_path) == []


@pytest.mark.asyncio
async def test_resolve_pending_reject_discards(db_path):
    strong = [Candidate("macd_divergence|bullish", wins=144, n=200)]
    for _ in range(gs.PROMOTE_AFTER):
        await gs.update_gating_state(strong, veto_mode=True, db_path=db_path)
    res = await gs.resolve_pending("macd_divergence|bullish", approve=False, db_path=db_path)
    assert res["status"] == "rejected"
    active = await gs.get_active_gating(db_path=db_path)
    assert "macd_divergence|bullish" not in active["promoted"]
