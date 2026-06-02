#!/usr/bin/env python3
"""Direct walk-forward backtest harness — no backend / no DB required.

Calls `backtester_walk_forward.run_walk_forward` directly so we can quickly
sanity-check the new realistic-cost evaluator + Monte Carlo + regime mutes
against historical NSE data without standing up the full API.

Usage:
    python3 scripts/run_walk_forward_test.py                 # default NIFTY top-10, 1y
    python3 scripts/run_walk_forward_test.py --period 2y
    python3 scripts/run_walk_forward_test.py --symbols TCS,INFY,HDFCBANK
    python3 scripts/run_walk_forward_test.py --period 5y --folds 4
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import statistics
from datetime import datetime
from pathlib import Path

# Make sure we can import the `app` package regardless of CWD.
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

# Default universe — a deliberately liquid mix across sectors so the run
# isn't dominated by a single mover and we see regime variety.
DEFAULT_SYMBOLS = [
    "RELIANCE",  # Energy
    "TCS",       # IT
    "HDFCBANK",  # Private bank
    "INFY",      # IT
    "ICICIBANK", # Private bank
    "SBIN",      # PSU bank
    "ITC",       # FMCG
    "TATAMOTORS",# Auto
    "MARUTI",    # Auto
    "HINDALCO",  # Metals
]


def _fmt_pct(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x:+.2f}%" if abs(x) >= 0.01 else f"{x:+.4f}%"


def _color(s: str, code: str) -> str:
    return f"\033[{code}m{s}\033[0m"


def _verdict_color(wr: float | None) -> str:
    if wr is None:
        return "—"
    s = f"{wr:.1f}%"
    if wr >= 55: return _color(s, "32")  # green
    if wr >= 50: return _color(s, "33")  # yellow
    if wr >= 45: return _color(s, "93")  # bright yellow
    return _color(s, "31")               # red


async def _one_symbol(sym: str, *, period: str, folds: int, eval_windows: list[int]) -> dict:
    from app.services.backtester_walk_forward import run_walk_forward
    try:
        res = await run_walk_forward(
            sym, period=period, n_folds=folds, eval_windows=eval_windows,
        )
        return res
    except Exception as e:
        return {"symbol": sym, "error": str(e)}


async def main_async(args: argparse.Namespace) -> int:
    symbols = args.symbols.split(",") if args.symbols else DEFAULT_SYMBOLS
    eval_windows = [int(w) for w in args.eval_windows.split(",")]

    print()
    print(_color("═" * 100, "36"))
    print(_color(f"  agentX walk-forward backtest", "1;36"))
    print(_color(f"  symbols={len(symbols)}  period={args.period}  folds={args.folds}  eval={eval_windows}", "36"))
    print(_color(f"  cost-model: realistic (apply_costs) + slippage (adv-aware)  · regime: v2 + mutes active", "36"))
    print(_color("═" * 100, "36"))
    print()

    headers = ["SYMBOL", "TRADES", "WR-5d", "WLB-5d", "MC-WR-p50", "MC-WR-p5", "AVG-5d", "MAX-DD", "BEST", "WORST"]
    print(f"{headers[0]:<14}{headers[1]:>8}{headers[2]:>10}{headers[3]:>10}"
          f"{headers[4]:>12}{headers[5]:>11}{headers[6]:>11}{headers[7]:>10}"
          f"  {headers[8]:<22}{headers[9]:<22}")
    print(_color("─" * 100, "90"))

    results: list[dict] = []
    for sym in symbols:
        res = await _one_symbol(sym, period=args.period, folds=args.folds, eval_windows=eval_windows)
        results.append(res)

        if "error" in res:
            print(f"{sym:<14}{_color(res['error'][:40], '31')}")
            continue

        # Pool raw trades across all folds, recompute WR/PnL honestly
        # by direction so a stock that ripped up doesn't get tagged as
        # "no edge" purely because the engine emitted bearish signals.
        folds_data = res.get("folds") or []
        all_trades_local: list[dict] = []
        for f in folds_data:
            all_trades_local.extend(f.get("trades") or [])

        def _stats(direction_filter=None):
            xs = [t for t in all_trades_local
                  if (direction_filter is None or t.get("direction") == direction_filter)
                  and "pnl_5d" in t and not t.get("neutral_5d", False)]
            if not xs:
                return None
            wins = sum(1 for t in xs if t.get("win_5d"))
            losses = len(xs) - wins
            pnls = [t["pnl_5d"] for t in xs]
            avg = statistics.mean(pnls)
            wr = wins / len(xs) * 100.0
            # Wilson LB
            n = len(xs); p = wins / n; z = 1.96
            denom = 1 + z*z/n
            centre = p + z*z/(2*n)
            margin = z * math.sqrt(max(0.0, (p*(1-p) + z*z/(4*n)) / n))
            wlb = max(0.0, (centre - margin) / denom) * 100.0
            # Compounded drawdown (cap a single trade impact at ±50% so a
            # blow-up in a thin name doesn't break the maths).
            equity = []
            cum_log = 0.0
            for r in pnls:
                r_capped = max(-50.0, min(50.0, r))
                cum_log += math.log(1.0 + r_capped / 100.0)
                equity.append(cum_log)
            peak = -1e18; max_dd = 0.0
            for v in equity:
                peak = max(peak, v)
                max_dd = min(max_dd, v - peak)
            # Convert log-drawdown → percent loss from peak
            dd_pct = (math.exp(max_dd) - 1.0) * 100.0
            return {"n": len(xs), "wr": wr, "wlb": wlb, "avg": avg, "dd_pct": dd_pct}

        agg = _stats(None)
        bulls = _stats("bullish")
        bears = _stats("bearish")

        if agg is None:
            print(f"{sym:<14}{'no trades':>20}")
            res["_summary"] = None
            continue

        # Pull MC from any fold (we computed per-fold).
        mc_p50 = mc_p5 = None
        for f in folds_data:
            m = f.get("metrics") or {}
            if m.get("mc_wr_p50_5d") is not None:
                mc_p50 = m["mc_wr_p50_5d"]; break
        for f in folds_data:
            m = f.get("metrics") or {}
            if m.get("mc_wr_p5_5d") is not None:
                mc_p5 = m["mc_wr_p5_5d"]; break

        # Best / worst signal_type — compute now from pooled trades.
        from collections import defaultdict
        by_type: dict[str, list[float]] = defaultdict(list)
        for t in all_trades_local:
            if "pnl_5d" in t and not t.get("neutral_5d", False):
                by_type[t.get("signal_type", "?")].append(t["pnl_5d"])
        type_means = {k: statistics.mean(v) for k, v in by_type.items() if len(v) >= 5}
        best_type = max(type_means, key=type_means.get) if type_means else "—"
        worst_type = min(type_means, key=type_means.get) if type_means else "—"

        wlb_str = _color(f"{agg['wlb']:.1f}%", "90")
        dd_str = _color(f"{agg['dd_pct']:.1f}%", "31")
        avg_str = _color(_fmt_pct(agg['avg']), '32' if agg['avg'] > 0 else '31')
        mc50_str = _color(f"{mc_p50:.1f}%", "90") if mc_p50 is not None else "—"
        mc5_str = _color(f"{mc_p5:.1f}%", "90") if mc_p5 is not None else "—"
        print(
            f"{sym:<14}{agg['n']:>8}"
            f"{_verdict_color(agg['wr']):>17}"
            f"{wlb_str:>17}"
            f"{mc50_str:>19}"
            f"{mc5_str:>18}"
            f"{avg_str:>20}"
            f"{dd_str:>17}"
            f"  {str(best_type)[:20]:<22}{str(worst_type)[:20]:<22}"
        )
        if bulls and bears:
            bull_line = f"  ↳ bull n={bulls['n']} WR={bulls['wr']:.1f}% avg={_fmt_pct(bulls['avg'])} / bear n={bears['n']} WR={bears['wr']:.1f}% avg={_fmt_pct(bears['avg'])}"
            print(f"{'':<14}{'':>8}{_color(bull_line, '36')}")

        res["_summary"] = agg

    print(_color("─" * 100, "90"))

    # Universe-level summary — pool raw trades across symbols.
    valid = [r for r in results if "error" not in r and r.get("_summary")]
    if not valid:
        print(_color("All symbols errored — likely a data-fetch issue (yfinance / network).", "31"))
        return 1

    total_trades = sum(r["_summary"]["n"] for r in valid)
    universe_wr = [r["_summary"]["wr"] for r in valid]
    universe_avg_pnl = [r["_summary"]["avg"] for r in valid]
    universe_mc_p5 = []
    for r in valid:
        for f in r.get("folds") or []:
            m = f.get("metrics") or f
            if m.get("mc_wr_p5_5d") is not None:
                universe_mc_p5.append(m["mc_wr_p5_5d"])

    print()
    print(_color("UNIVERSE SUMMARY", "1;36"))
    print(_color("─" * 50, "36"))
    print(f"  Symbols evaluated     : {len(valid)} / {len(symbols)}")
    print(f"  Total directional trades: {total_trades}")
    if universe_wr:
        wr_mean = statistics.mean(universe_wr)
        print(f"  Avg 5d win rate (folds): {_verdict_color(wr_mean)}")
    if universe_avg_pnl:
        m = statistics.mean(universe_avg_pnl)
        print(f"  Avg 5d net P&L         : {_color(_fmt_pct(m), '32' if m > 0 else '31')}")
    if universe_mc_p5:
        m = statistics.mean(universe_mc_p5)
        print(f"  Avg MC p5 WR (5d)      : {_color(f'{m:.1f}%', '90')}   "
              f"{_color('(p5 < 45% → fragile per ADR-9)', '90')}")

    # Verdict
    print()
    if universe_wr:
        wr_mean = statistics.mean(universe_wr)
        if wr_mean >= 55 and universe_mc_p5 and statistics.mean(universe_mc_p5) >= 50:
            print(_color("  VERDICT: ✓ Edge confirmed (>55% WR, MC p5 ≥ 50%)", "1;32"))
        elif wr_mean >= 50:
            print(_color("  VERDICT: ~ Marginal — needs the LLM layers + recalibration", "1;33"))
        elif wr_mean >= 45:
            print(_color("  VERDICT: ✗ Below cost-adjusted break-even (45-50%)", "1;33"))
        else:
            print(_color("  VERDICT: ✗ No edge in raw signal engine — LLM layers must carry it", "1;31"))
    print(_color("─" * 50, "36"))

    # Per-signal-type aggregate across the universe — answers "which
    # detector actually wins across symbols". The 9pt.md cohort dashboard
    # surfaces this in production; here we compute it from raw trades.
    print()
    print(_color("PER-SIGNAL-TYPE EDGE (across universe, 5d)", "1;36"))
    print(_color("─" * 80, "36"))
    print(f"  {'SIGNAL_TYPE':<26}{'DIR':<10}{'N':>6}{'WR':>10}{'WLB':>10}{'AVG':>12}")
    from collections import defaultdict
    pool: dict[tuple[str, str], list[float]] = defaultdict(list)
    pool_wins: dict[tuple[str, str], int] = defaultdict(int)
    for r in valid:
        for f in r.get("folds") or []:
            for t in f.get("trades") or []:
                if "pnl_5d" not in t or t.get("neutral_5d"):
                    continue
                key = (t.get("signal_type", "?"), t.get("direction", "?"))
                pool[key].append(t["pnl_5d"])
                if t.get("win_5d"):
                    pool_wins[key] += 1
    rows = []
    for k, pnls in pool.items():
        n = len(pnls)
        if n < 10:  # too few to learn from
            continue
        wins = pool_wins[k]
        wr = wins / n * 100.0
        p = wins / n; z = 1.96
        denom = 1 + z*z/n
        centre = p + z*z/(2*n)
        margin = z * math.sqrt(max(0.0, (p*(1-p) + z*z/(4*n)) / n))
        wlb = max(0.0, (centre - margin) / denom) * 100.0
        rows.append((k[0], k[1], n, wr, wlb, statistics.mean(pnls)))
    rows.sort(key=lambda r: r[3], reverse=True)
    for sig_type, direction, n, wr, wlb, avg in rows:
        color = "32" if wr >= 55 else ("33" if wr >= 50 else ("93" if wr >= 45 else "31"))
        print(f"  {sig_type[:25]:<26}{direction[:9]:<10}{n:>6}"
              f"{_color(f'{wr:.1f}%', color):>17}{_color(f'{wlb:.1f}%', '90'):>17}"
              f"{_color(_fmt_pct(avg), '32' if avg > 0 else '31'):>19}")

    # "What if" — re-aggregate the universe after muting every
    # (signal_type, direction) whose mean net P&L is < 0 on n ≥ 30.
    # This is the actually-actionable cohort decision rule: kill the
    # losers, keep the winners and the under-sampled. Equivalent to
    # what `signal_edge.DIRECTIONAL_MUTES` codifies in production.
    muted_keys = set()
    for k, pnls in pool.items():
        if len(pnls) >= 30 and statistics.mean(pnls) < 0:
            muted_keys.add((k[0], k[1]))
    if muted_keys:
        post_wins = post_n = 0
        post_pnls: list[float] = []
        for r in valid:
            for f in r.get("folds") or []:
                for t in f.get("trades") or []:
                    if "pnl_5d" not in t or t.get("neutral_5d"):
                        continue
                    key = (t.get("signal_type", "?"), t.get("direction", "?"))
                    if key in muted_keys:
                        continue
                    post_n += 1
                    if t.get("win_5d"):
                        post_wins += 1
                    post_pnls.append(t["pnl_5d"])
        if post_n > 0:
            post_wr = post_wins / post_n * 100.0
            post_avg = statistics.mean(post_pnls) if post_pnls else 0.0
            print()
            print(_color(f"WHAT-IF: drop {len(muted_keys)} (type,dir) combos with WLB < 40%", "1;33"))
            print(f"  Universe size  : {total_trades} → {post_n} trades  ({(post_n/total_trades*100):.0f}% retained)")
            print(f"  Universe WR    : {statistics.mean(universe_wr):.1f}% → {_verdict_color(post_wr)}")
            print(f"  Universe avg P&L: {statistics.mean(universe_avg_pnl):+.2f}% → "
                  f"{_color(_fmt_pct(post_avg), '32' if post_avg > 0 else '31')}")
            print(_color(f"  Muted combos: " + ", ".join(f"{a}/{b}" for a,b in sorted(muted_keys)), "90"))

    # Ceiling — what if we kept ONLY combos with positive avg P&L AND n>=15?
    keep_keys = {k for k, pnls in pool.items()
                 if len(pnls) >= 15 and statistics.mean(pnls) > 0}
    if keep_keys:
        ideal_wins = ideal_n = 0
        ideal_pnls: list[float] = []
        for r in valid:
            for f in r.get("folds") or []:
                for t in f.get("trades") or []:
                    if "pnl_5d" not in t or t.get("neutral_5d"):
                        continue
                    key = (t.get("signal_type", "?"), t.get("direction", "?"))
                    if key not in keep_keys:
                        continue
                    ideal_n += 1
                    if t.get("win_5d"):
                        ideal_wins += 1
                    ideal_pnls.append(t["pnl_5d"])
        if ideal_n > 0:
            ideal_wr = ideal_wins / ideal_n * 100.0
            ideal_avg = statistics.mean(ideal_pnls)
            print()
            print(_color(f"CEILING: keep ONLY {len(keep_keys)} positive-avg combos (n>=15)", "1;32"))
            print(f"  Universe size  : {total_trades} → {ideal_n} trades  ({(ideal_n/total_trades*100):.0f}% retained)")
            print(f"  Universe WR    : {statistics.mean(universe_wr):.1f}% → {_verdict_color(ideal_wr)}")
            print(f"  Universe avg P&L: {statistics.mean(universe_avg_pnl):+.2f}% → "
                  f"{_color(_fmt_pct(ideal_avg), '32' if ideal_avg > 0 else '31')}")
            print(_color(f"  Kept combos: " + ", ".join(f"{a}/{b}" for a,b in sorted(keep_keys)), "90"))

    # Save JSON for follow-up.
    if args.output:
        Path(args.output).write_text(json.dumps(results, default=str, indent=2))
        print(f"\n  Raw results: {args.output}")

    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--period", default="1y", help="yfinance period (1y/2y/5y)")
    p.add_argument("--folds", type=int, default=4, help="walk-forward folds")
    p.add_argument("--symbols", default="", help="comma-separated symbols (overrides default)")
    p.add_argument("--eval-windows", default="1,3,5,10", dest="eval_windows")
    p.add_argument("--output", default=f"backtest_results/walk_fwd_{datetime.now():%Y%m%d_%H%M%S}.json")
    args = p.parse_args()

    Path("backtest_results").mkdir(exist_ok=True)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
