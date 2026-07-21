from __future__ import annotations
"""A1 + A2 — autonomous gating: derive promote/mute/block sets with a state machine.

Replaces the hand-typed constants in `signal_edge.py` (SYMBOL_BLOCKLIST,
DIRECTIONAL_MUTES, PROMOTED_SIGNALS) with sets *derived from data* — but only
behind two protections so the autonomous loop can't overfit:

  * **A3 guardrails** (`multiple_testing.select_significant`): a combo's win
    record must be FDR-significant above break-even on an adequate sample
    before it is even eligible for promotion.
  * **Hysteresis state machine** (this module): eligibility on a *single*
    round is not enough. A combo must pass on ``PROMOTE_AFTER`` consecutive
    weekly rounds to become PROMOTED, and a PROMOTED combo is demoted after
    ``DEMOTE_AFTER`` consecutive failures. This is what stops the gate from
    flip-flopping on noise and is the mechanism that replaces a human reading
    the backtest each week.

States: CANDIDATE → (consecutive passes) → PROMOTED, and back to CANDIDATE on
sustained failure; a combo whose win rate sits materially *below* break-even on
a large sample is MUTED. Per-symbol records use the same machine with a BLOCKED
terminal state.

The derived sets are persisted to `gating_state`; `get_active_gating()` returns
them for the engine to consume. Seeding from the existing hand-curated
constants is supported so the first run starts where the human left off.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite

from app.database import DB_PATH
from app.services.multiple_testing import (
    Candidate, select_significant, wilson_lower_bound,
)

logger = logging.getLogger(__name__)

PROMOTE_AFTER = 3   # consecutive significant rounds to reach PROMOTED
DEMOTE_AFTER = 2    # consecutive failures to leave PROMOTED
MUTE_WINRATE = 0.45     # win rate below this on a large sample → MUTED
MUTE_MIN_TRADES = 100   # sample needed before a MUTE is trusted

# ── 1.3 Per-signal pre-registered kill criteria ──────────────
# A (signal_type, direction) combo dies AUTOMATICALLY — terminal `killed`
# state, no re-entry — once it has enough LIVE (forward) trades and even the
# optimistic (Wilson lower-bound) case for its win rate is below the
# pre-registered floor. Pre-registered here, in code, so it can't be moved
# after seeing the data. `killed` is terminal; only wiping the row (a fresh
# forward test) revives the combo.
KILL_MIN_LIVE_TRADES = 50   # live trades required before a kill can fire
KILL_WILSON_LB = 0.40       # Wilson-LB win rate below this on ≥50 live → KILLED

# Demotion is irreversible without FRESH forward evidence: once a combo has
# ever left PROMOTED (or been MUTED), backtest passes alone cannot re-promote
# it. Re-promotion additionally requires a live record clearing this bar — so a
# refuted edge can't quietly re-enter on the same in-sample signal that the
# forward test already contradicted.
REPROMOTE_MIN_LIVE = 30     # live trades required to overturn a demotion
REPROMOTE_WILSON_LB = 0.50  # live Wilson-LB must clear break-even to re-promote

CREATE_GATING_STATE_TABLE = """
CREATE TABLE IF NOT EXISTS gating_state (
    key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    consecutive_pass INTEGER DEFAULT 0,
    consecutive_fail INTEGER DEFAULT 0,
    last_win_rate REAL,
    last_n INTEGER,
    last_p_value REAL,
    updated_at TEXT,
    history_json TEXT,
    demoted_ever INTEGER DEFAULT 0
);
"""

# A5: proposed transitions awaiting human approval (veto mode). A pending row
# means the state machine WANTED to change state but is holding for a human.
CREATE_GATING_PENDING_TABLE = """
CREATE TABLE IF NOT EXISTS gating_pending (
    key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    win_rate REAL,
    n INTEGER,
    proposed_at TEXT
);
"""


async def _ensure(db: aiosqlite.Connection) -> None:
    await db.execute(CREATE_GATING_STATE_TABLE)
    await db.execute(CREATE_GATING_PENDING_TABLE)
    # Migration: add demoted_ever to pre-existing tables (idempotent).
    async with db.execute("PRAGMA table_info(gating_state)") as cur:
        cols = {r[1] for r in await cur.fetchall()}
    if "demoted_ever" not in cols:
        await db.execute(
            "ALTER TABLE gating_state ADD COLUMN demoted_ever INTEGER DEFAULT 0")


def _next_state(
    current: str, *, passed: bool, win_rate: float, n: int,
    consecutive_pass: int, consecutive_fail: int,
    live_wins: int = 0, live_n: int = 0, demoted_ever: bool = False,
) -> tuple[str, int, int, bool]:
    """Pure transition function.

    Returns (state, consecutive_pass, consecutive_fail, demoted_ever).

    Priority of rules:
      1. ``killed`` is terminal — a killed combo never transitions again.
      2. Per-signal kill: ≥ KILL_MIN_LIVE_TRADES live trades with a live
         Wilson-LB below KILL_WILSON_LB ⇒ KILLED (automatic, pre-registered).
      3. Hysteresis promote/demote as before, BUT a combo that was ever
         demoted needs fresh forward evidence (live Wilson-LB ≥ REPROMOTE
         bar on ≥ REPROMOTE_MIN_LIVE trades) to re-promote — backtest passes
         alone can't revive a forward-refuted edge.
    """
    if current == "killed":
        return "killed", consecutive_pass, consecutive_fail, demoted_ever

    # Rule 2 — automatic kill on a well-sampled, forward-refuted combo.
    if live_n >= KILL_MIN_LIVE_TRADES and wilson_lower_bound(live_wins, live_n) < KILL_WILSON_LB:
        return "killed", 0, consecutive_fail + 1, True

    forward_ok = (live_n >= REPROMOTE_MIN_LIVE
                  and wilson_lower_bound(live_wins, live_n) >= REPROMOTE_WILSON_LB)

    if passed:
        consecutive_pass += 1
        consecutive_fail = 0
        if consecutive_pass >= PROMOTE_AFTER and current != "promoted":
            # Irreversibility: a previously-demoted combo may only re-promote
            # with fresh forward evidence; otherwise it stays a candidate.
            if demoted_ever and not forward_ok:
                return "candidate", consecutive_pass, consecutive_fail, demoted_ever
            return "promoted", consecutive_pass, consecutive_fail, demoted_ever
        if current == "promoted":
            return "promoted", consecutive_pass, consecutive_fail, demoted_ever
        # A passing combo that isn't yet promoted is a candidate (clears MUTE).
        return "candidate", consecutive_pass, consecutive_fail, demoted_ever

    # Did not pass this round.
    consecutive_fail += 1
    consecutive_pass = 0
    if current == "promoted":
        if consecutive_fail >= DEMOTE_AFTER:
            # Demotion — permanently flag so re-promotion needs forward proof.
            return "candidate", consecutive_pass, consecutive_fail, True
        return "promoted", consecutive_pass, consecutive_fail, demoted_ever  # grace
    # Mute only on a clear, well-sampled negative — not mere non-significance.
    if n >= MUTE_MIN_TRADES and win_rate < MUTE_WINRATE:
        return "muted", consecutive_pass, consecutive_fail, True
    return ("muted" if current == "muted" else "candidate",
            consecutive_pass, consecutive_fail, demoted_ever)


async def update_gating_state(
    round_results: list[Candidate],
    *,
    kind: str = "combo",
    p0: float = 0.5,
    alpha: float = 0.05,
    min_trades: int = 30,
    veto_mode: bool = False,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Run one round of the state machine over derived win records.

    `round_results` is this round's (key, wins, n) per combo/symbol — typically
    mapped from the weekly walk-forward. Applies A3 significance, advances each
    key's state with hysteresis, persists, and returns a transition summary.
    """
    path = db_path or DB_PATH
    verdicts = {v.key: v for v in select_significant(
        round_results, p0=p0, alpha=alpha, min_trades=min_trades)}
    now = datetime.now(timezone.utc).isoformat()
    transitions: list[dict[str, Any]] = []

    async with aiosqlite.connect(path) as db:
        await _ensure(db)
        db.row_factory = aiosqlite.Row
        for cand in round_results:
            v = verdicts.get(cand.key)
            passed = bool(v and v.passed)
            wr = (cand.wins / cand.n) if cand.n > 0 else 0.0
            async with db.execute(
                "SELECT state, consecutive_pass, consecutive_fail, history_json, "
                "demoted_ever FROM gating_state WHERE key = ?", (cand.key,)
            ) as cur:
                row = await cur.fetchone()
            current = row["state"] if row else "candidate"
            cp = row["consecutive_pass"] if row else 0
            cf = row["consecutive_fail"] if row else 0
            demoted_ever = bool(row["demoted_ever"]) if row else False
            try:
                history = json.loads(row["history_json"]) if row and row["history_json"] else []
            except Exception:
                history = []

            new_state, cp, cf, demoted_ever = _next_state(
                current, passed=passed, win_rate=wr, n=cand.n,
                consecutive_pass=cp, consecutive_fail=cf,
                live_wins=cand.live_wins, live_n=cand.live_n,
                demoted_ever=demoted_ever)

            # A5: in veto mode, a state CHANGE is held as a pending proposal —
            # the live state does not move until a human approves. Counters
            # still advance (so approval reflects accumulated evidence).
            if new_state != current and veto_mode:
                await db.execute(
                    "INSERT INTO gating_pending (key, kind, from_state, to_state, "
                    "win_rate, n, proposed_at) VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET to_state=excluded.to_state, "
                    "win_rate=excluded.win_rate, n=excluded.n, proposed_at=excluded.proposed_at",
                    (cand.key, kind, current, new_state, round(wr, 4), cand.n, now),
                )
                transitions.append({"key": cand.key, "from": current,
                                    "to": new_state, "pending": True})
                new_state = current  # hold the live state

            if new_state != current:
                history.append({"at": now, "from": current, "to": new_state,
                                "wr": round(wr, 4), "n": cand.n})
                transitions.append({"key": cand.key, "from": current, "to": new_state})

            await db.execute(
                "INSERT INTO gating_state (key, kind, state, consecutive_pass, "
                "consecutive_fail, last_win_rate, last_n, last_p_value, updated_at, "
                "history_json, demoted_ever) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET state=excluded.state, "
                "consecutive_pass=excluded.consecutive_pass, "
                "consecutive_fail=excluded.consecutive_fail, "
                "last_win_rate=excluded.last_win_rate, last_n=excluded.last_n, "
                "last_p_value=excluded.last_p_value, updated_at=excluded.updated_at, "
                "history_json=excluded.history_json, demoted_ever=excluded.demoted_ever",
                (cand.key, kind, new_state, cp, cf, round(wr, 4), cand.n,
                 round(v.p_value, 6) if v else None, now,
                 json.dumps(history[-50:]), int(demoted_ever)),
            )
        await db.commit()

    return {"evaluated": len(round_results), "transitions": transitions}


async def get_active_gating(*, db_path: Optional[str] = None) -> dict[str, Any]:
    """Derived gating sets for the engine: promoted / muted / blocked keys.

    Returns sets of keys (``"signal_type|direction"`` or ``"SYMBOL"``). Empty
    sets when nothing has been derived yet — the engine then falls back to its
    hand-curated constants, so this is safe to consult unconditionally.
    """
    path = db_path or DB_PATH
    out = {"promoted": set(), "muted": set(), "blocked": set(),
           "candidate": set(), "killed": set()}
    try:
        async with aiosqlite.connect(path) as db:
            await _ensure(db)
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT key, state FROM gating_state") as cur:
                for row in await cur.fetchall():
                    out.setdefault(row["state"], set()).add(row["key"])
    except Exception as e:
        logger.debug("get_active_gating failed: %s", e)
    return out


async def list_pending(*, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    """A5: proposed transitions awaiting human approval."""
    path = db_path or DB_PATH
    try:
        async with aiosqlite.connect(path) as db:
            await _ensure(db)
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT key, kind, from_state, to_state, win_rate, n, proposed_at "
                "FROM gating_pending ORDER BY proposed_at DESC"
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
    except Exception as e:
        logger.debug("list_pending failed: %s", e)
        return []


async def resolve_pending(
    key: str, approve: bool, *, db_path: Optional[str] = None
) -> dict[str, Any]:
    """A5: approve (apply the proposed transition) or reject (discard) a proposal."""
    path = db_path or DB_PATH
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(path) as db:
        await _ensure(db)
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM gating_pending WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return {"status": "not_found", "key": key}
        if approve:
            await db.execute(
                "UPDATE gating_state SET state = ?, updated_at = ? WHERE key = ?",
                (row["to_state"], now, key),
            )
        await db.execute("DELETE FROM gating_pending WHERE key = ?", (key,))
        await db.commit()
    return {"status": "approved" if approve else "rejected", "key": key,
            "to_state": row["to_state"] if approve else None}


# ── sync overlay cache (mirrors signal_edge._edge_overrides) ──
# signal_edge's predicates are sync and hot-path; this in-memory overlay lets
# them consult the derived sets without an await. Seed at startup via
# seed_overlay(); the overlay predicates return None when a key is in no
# derived set, so the caller falls back to its hand-curated constant.
_overlay: dict[str, set] = {"promoted": set(), "muted": set(), "blocked": set()}


def set_overlay(sets: dict[str, set]) -> None:
    global _overlay
    # `killed` combos are permanently un-tradeable — fold them into the muted
    # overlay so the sync hot-path (which already respects mutes) blocks them
    # without needing a separate predicate.
    muted = set(sets.get("muted") or ()) | set(sets.get("killed") or ())
    _overlay = {
        "promoted": set(sets.get("promoted") or ()),
        "muted": muted,
        "blocked": set(sets.get("blocked") or ()),
    }


async def seed_overlay(*, db_path: Optional[str] = None) -> int:
    """Load derived sets into the sync overlay cache. Call at startup."""
    active = await get_active_gating(db_path=db_path)
    set_overlay(active)
    return sum(len(active.get(k, ())) for k in ("promoted", "muted", "blocked", "killed"))


def overlay_is_promoted(signal_type: str, direction: str) -> Optional[bool]:
    """True if derived-promoted, None if no derived opinion (→ use constant)."""
    if not _overlay["promoted"] and not _overlay["muted"]:
        return None
    return f"{signal_type}|{direction}" in _overlay["promoted"]


def overlay_is_muted(signal_type: str, direction: str) -> Optional[bool]:
    if not _overlay["muted"] and not _overlay["promoted"]:
        return None
    return f"{signal_type}|{direction}" in _overlay["muted"]


def overlay_is_blocked(symbol: str) -> Optional[bool]:
    if not _overlay["blocked"]:
        return None
    return (symbol or "").upper() in _overlay["blocked"]


async def seed_from_constants(
    promoted: list[tuple[str, str]],
    muted: list[tuple[str, str]],
    blocked: list[str],
    *,
    db_path: Optional[str] = None,
) -> int:
    """One-time seed of the state table from the existing hand-curated sets.

    HONESTY FIX (2026-06): hand-curated *promotions* are seeded as **candidate**,
    not promoted. Seeding them 'promoted' meant the FDR machine inherited an
    in-sample edge it never had to earn — it could only ever demote, never
    validate, so the amplification outlived its own forward refutation. Now a
    promotion must be EARNED by the gate (3 FDR-significant disjoint rounds).
    Mutes and blocks are still inherited as-is: they only ever REDUCE risk, so
    starting conservative is safe. Idempotent — only inserts new keys.
    """
    path = db_path or DB_PATH
    now = datetime.now(timezone.utc).isoformat()
    rows = (
        [(f"{s}|{d}", "combo", "candidate") for s, d in promoted]
        + [(f"{s}|{d}", "combo", "muted") for s, d in muted]
        + [(s, "symbol", "blocked") for s in blocked]
    )
    n = 0
    async with aiosqlite.connect(path) as db:
        await _ensure(db)
        for key, kind, state in rows:
            cur = await db.execute(
                "INSERT OR IGNORE INTO gating_state (key, kind, state, updated_at, "
                "history_json) VALUES (?,?,?,?,?)",
                (key, kind, state, now, json.dumps([{"at": now, "seed": state}])),
            )
            n += cur.rowcount or 0
        await db.commit()
    return n
