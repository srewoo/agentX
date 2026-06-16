#!/usr/bin/env python3
"""Data-quality experiment: does the QV edge survive removing look-ahead bias?

Runs the quality/value walk-forward backtest twice over the same universe and
period — once in the legacy biased mode, once in point-in-time mode — and
prints the win-rate / expectancy delta plus exactly which biases were actually
corrected. This is the experiment that answers: *is the backtest edge real, or
an artifact of restated fundamentals + survivorship?*

Honest by construction: if FMP point-in-time data isn't available (no key /
restricted plan) or no constituents-history CSV is present, the run says so and
the comparison degrades to "could not correct bias X" rather than silently
claiming a clean read.

Usage:
    python -m scripts.data_quality_experiment --period 3y --limit 40
    python -m scripts.data_quality_experiment --symbols RELIANCE,TCS,INFY
"""
from __future__ import annotations
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _c(s, code):  # tiny ANSI helper
    return f"\033[{code}m{s}\033[0m"


def _summary_line(label: str, res: dict) -> str:
    s = res.get("universe_summary", {}) or {}
    m = res.get("methodology", {}) or {}
    return (
        f"{label:18} trades={s.get('trades', 0):5} "
        f"WR>0={s.get('win_rate_pos', 'NA')!s:>6} "
        f"WR>10%={s.get('win_rate_gt_10pct', 'NA')!s:>6} "
        f"avgPnL={s.get('avg_pnl_pct', 'NA')!s:>7} "
        f"sharpe={s.get('sharpe', 'NA')!s:>6}  "
        f"[PIT={m.get('pit_fundamentals_applied')} "
        f"surv_free={m.get('survivorship_free')} "
        f"cover={m.get('pit_symbol_coverage')}]"
    )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="3y")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--symbols", default=None, help="comma-separated; overrides --limit")
    ap.add_argument("--rebalance-days", type=int, default=60)
    args = ap.parse_args()

    from app.services.quality_value_backtester import run_qv_walk_forward
    from app.services.universe_pit import has_constituent_history

    symbols = None
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.limit:
        from app.services.data_fetcher import MAJOR_STOCKS
        symbols = [s["symbol"] for s in MAJOR_STOCKS if not s["symbol"].startswith("^")][: args.limit]

    print(_c("\n=== DATA-QUALITY EXPERIMENT: biased vs point-in-time ===", "1;36"))
    print(f"period={args.period}  symbols={len(symbols) if symbols else 'default'}  "
          f"constituents_history_csv={'present' if has_constituent_history() else 'ABSENT'}\n")

    common = dict(symbols=symbols, period=args.period, rebalance_days=args.rebalance_days)

    print("Running legacy (biased) backtest…")
    biased = await run_qv_walk_forward(**common, point_in_time=False)
    print("Running point-in-time backtest…")
    pit = await run_qv_walk_forward(**common, point_in_time=True)

    print("\n" + _c("RESULTS", "1;37"))
    print(_summary_line("legacy (biased)", biased))
    print(_summary_line("point-in-time", pit))

    bs = biased.get("universe_summary", {}) or {}
    ps = pit.get("universe_summary", {}) or {}
    pm = pit.get("methodology", {}) or {}

    def _num(x):
        try:
            return float(x)
        except Exception:
            return None

    bw, pw = _num(bs.get("win_rate_pos")), _num(ps.get("win_rate_pos"))
    ba, pa = _num(bs.get("avg_pnl_pct")), _num(ps.get("avg_pnl_pct"))

    print("\n" + _c("DELTA (point-in-time minus legacy)", "1;37"))
    if bw is not None and pw is not None:
        print(f"  win-rate>0:  {pw - bw:+.2f} pp   ({bw:.1f}% -> {pw:.1f}%)")
    if ba is not None and pa is not None:
        print(f"  avg PnL:     {pa - ba:+.3f} pp   ({ba:.3f}% -> {pa:.3f}%)")

    corrected = pm.get("biases_corrected", [])
    print("\n" + _c("BIAS CORRECTION ACTUALLY APPLIED", "1;37"))
    print(f"  corrected: {corrected or 'NONE'}")
    for b in pm.get("known_biases", []):
        print(f"  {_c('still biased:', '33')} {b}")

    print("\n" + _c("VERDICT", "1;37"))
    if not corrected:
        print(_c("  Inconclusive — no bias could be corrected this run.", "31"))
        print("  Add an FMP key with statement access and/or a constituents-history")
        print("  CSV (backend/models/nse_constituents_history.csv), then re-run.")
    elif pa is not None and pa <= 0:
        print(_c("  Edge does NOT survive de-biasing — PIT expectancy <= 0.", "31"))
        print("  The backtest edge was (at least partly) a data artifact.")
        print("  Next move is new SIGNAL GENERATION, not more sizing/filtering.")
    elif pa is not None and ba is not None and pa < ba * 0.5:
        print(_c("  Edge shrinks materially under de-biasing — treat with caution.", "33"))
    else:
        print(_c("  Edge largely survives de-biasing — this is the encouraging case.", "32"))
        print("  Proceed to the live paper-trade watch with more confidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
