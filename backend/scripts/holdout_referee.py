#!/usr/bin/env python3
"""1.2 — Operator CLI for the final untouched holdout.

Three deliberate operator actions:

    python3 scripts/holdout_referee.py pin        # reserve last 12 months (once)
    python3 scripts/holdout_referee.py status     # show boundary + whether pinned
    python3 scripts/holdout_referee.py referee     # FINAL out-of-sample verdict run

`pin` is immutable — running it again is a no-op, so the reserved window can
never be silently moved after data has been seen. Every selection path
(walk-forward feeding gating, FDR promotion) already refuses to read past the
boundary; `referee` is the ONE run that deliberately reads the reserved window,
for the end-of-Phase-2 verdict. Its output should be recorded and treated as
immutable.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from app.services import holdout  # noqa: E402
from app.services.data_fetcher import MAJOR_STOCKS  # noqa: E402


async def _pin() -> dict:
    return await holdout.pin_boundary()


async def _status() -> dict:
    b = await holdout.resolve_boundary()
    return {"pinned": b is not None, "boundary": b.isoformat() if b else None,
            "months_reserved": holdout.HOLDOUT_MONTHS}


async def _referee(symbols: list[str], period: str, folds: int) -> dict:
    from app.services.backtester_walk_forward import run_universe_walk_forward

    b = await holdout.resolve_boundary()
    if b is None:
        return {"error": "no holdout pinned — run `pin` first; refusing a "
                         "referee run with nothing reserved"}
    # referee=True is the deliberate escape hatch that reads the reserved window.
    wf = await run_universe_walk_forward(
        symbols=symbols, period=period, n_folds=folds, referee=True)
    return {"holdout_boundary": b.isoformat(),
            "note": "REFEREE run — reads the reserved holdout. Record as immutable.",
            "oos_summary": wf.get("oos_summary"),
            "by_signal_type_oos": wf.get("by_signal_type_oos")}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["pin", "status", "referee"])
    ap.add_argument("--period", default="5y")
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--symbols", default=None,
                    help="comma-separated; defaults to first 40 NSE majors")
    args = ap.parse_args()

    if args.action == "pin":
        out = asyncio.run(_pin())
    elif args.action == "status":
        out = asyncio.run(_status())
    else:
        syms = ([s.strip().upper() for s in args.symbols.split(",")]
                if args.symbols else
                [s["symbol"] for s in MAJOR_STOCKS if not s["symbol"].startswith("^")][:40])
        out = asyncio.run(_referee(syms, args.period, args.folds))

    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
