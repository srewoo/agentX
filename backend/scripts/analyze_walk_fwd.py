#!/usr/bin/env python3
"""Post-hoc analysis on a saved walk_fwd_*.json file.

Pulls out questions the live harness doesn't answer:
  • Per-fold WR stability per symbol (is the system collapsing in a
    particular regime / time window?)
  • Per-horizon comparison (1d vs 3d vs 5d vs 10d) — which eval window
    extracts the most signal-engine edge?
  • Regime-conditional WR (the backtester tags `regime` on every trade)
  • Distribution of pnl_5d (skewness, fat tails)
  • Theoretical Kelly fraction per combo — sizing implication
"""
from __future__ import annotations
import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def _color(s, c): return f"\033[{c}m{s}\033[0m"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("path")
    args = p.parse_args()
    data = json.loads(Path(args.path).read_text())

    # 1. Per-symbol per-fold table
    print(_color("═" * 90, "36"))
    print(_color("  PER-SYMBOL PER-FOLD WR STABILITY (5d horizon)", "1;36"))
    print(_color("═" * 90, "36"))
    print(f"{'SYMBOL':<14}{'FOLD0':>10}{'FOLD1':>10}{'FOLD2':>10}{'FOLD3':>10}{'SPREAD':>10}{'COLLAPSE?':>15}")
    print("─" * 90)
    for sym_res in data:
        if "error" in sym_res:
            continue
        wrs = []
        for f in sym_res.get("folds") or []:
            m = f.get("metrics") or {}
            wrs.append(m.get("win_rate_5d"))
        if not wrs or all(w is None for w in wrs):
            continue
        valid = [w for w in wrs if w is not None]
        if not valid:
            continue
        spread = max(valid) - min(valid)
        collapse = ""
        if min(valid) < 30 and max(valid) > 50:
            collapse = _color("REGIME-FRAGILE", "31")
        elif spread > 30:
            collapse = _color("unstable", "33")
        else:
            collapse = _color("stable", "32")
        cells = []
        for w in wrs:
            if w is None:
                cells.append(f"{'—':>10}")
            else:
                col = "32" if w >= 55 else "33" if w >= 45 else "31"
                cells.append(_color(f"{w:>8.1f}%", col).rjust(18))
        print(f"{sym_res['symbol']:<14}" + "".join(cells) + f"{spread:>9.1f}%  {collapse}")

    # 2. Per-horizon WR — does a different eval window work better?
    print()
    print(_color("═" * 90, "36"))
    print(_color("  PER-HORIZON UNIVERSE WR (1d / 3d / 5d / 10d)", "1;36"))
    print(_color("═" * 90, "36"))
    horizons = [1, 3, 5, 10]
    for h in horizons:
        wins = losses = 0
        pnls = []
        for sym_res in data:
            for f in sym_res.get("folds") or []:
                for t in f.get("trades") or []:
                    win_key = f"win_{h}d"
                    pnl_key = f"pnl_{h}d"
                    neu_key = f"neutral_{h}d"
                    if pnl_key not in t or t.get(neu_key):
                        continue
                    if t.get(win_key):
                        wins += 1
                    else:
                        losses += 1
                    pnls.append(t[pnl_key])
        n = wins + losses
        if n == 0:
            continue
        wr = wins / n * 100.0
        avg = statistics.mean(pnls)
        med = statistics.median(pnls)
        col = "32" if wr >= 55 else "33" if wr >= 45 else "31"
        col2 = "32" if avg > 0 else "31"
        print(f"  {h:>2}d horizon  n={n:>5}  WR={_color(f'{wr:>5.1f}%', col)}  "
              f"avg={_color(f'{avg:+.2f}%', col2)}  median={med:+.2f}%")

    # 3. Regime-conditional WR
    print()
    print(_color("═" * 90, "36"))
    print(_color("  WR CONDITIONAL ON REGIME (5d, regime tagged at trade time)", "1;36"))
    print(_color("═" * 90, "36"))
    by_regime: dict[str, list[dict]] = defaultdict(list)
    for sym_res in data:
        for f in sym_res.get("folds") or []:
            for t in f.get("trades") or []:
                if "pnl_5d" not in t or t.get("neutral_5d"):
                    continue
                by_regime[t.get("regime", "?")].append(t)
    for regime in sorted(by_regime.keys()):
        ts = by_regime[regime]
        if len(ts) < 20:
            continue
        wins = sum(1 for t in ts if t.get("win_5d"))
        wr = wins / len(ts) * 100.0
        avg = statistics.mean(t["pnl_5d"] for t in ts)
        col = "32" if wr >= 55 else "33" if wr >= 45 else "31"
        col2 = "32" if avg > 0 else "31"
        bull = sum(1 for t in ts if t.get("direction") == "bullish")
        bear = sum(1 for t in ts if t.get("direction") == "bearish")
        print(f"  {regime:<15}  n={len(ts):>4}  WR={_color(f'{wr:>5.1f}%', col)}  "
              f"avg={_color(f'{avg:+.2f}%', col2)}   "
              f"(bull/bear: {bull}/{bear})")

    # 4. Distribution shape — fat tails?
    print()
    print(_color("═" * 90, "36"))
    print(_color("  P&L DISTRIBUTION (5d, all trades)", "1;36"))
    print(_color("═" * 90, "36"))
    all_pnls = []
    for sym_res in data:
        for f in sym_res.get("folds") or []:
            for t in f.get("trades") or []:
                if "pnl_5d" in t and not t.get("neutral_5d"):
                    all_pnls.append(t["pnl_5d"])
    if all_pnls:
        all_pnls.sort()
        n = len(all_pnls)
        def pctl(p): return all_pnls[max(0, min(n-1, int(p * (n-1))))]
        print(f"  n              : {n}")
        print(f"  mean           : {statistics.mean(all_pnls):+.2f}%")
        print(f"  median         : {statistics.median(all_pnls):+.2f}%")
        print(f"  stdev          : {statistics.stdev(all_pnls):.2f}%")
        print(f"  p5  / p95      : {pctl(0.05):+.2f}%  /  {pctl(0.95):+.2f}%")
        print(f"  p1  / p99      : {pctl(0.01):+.2f}%  /  {pctl(0.99):+.2f}%")
        print(f"  min / max      : {min(all_pnls):+.2f}%  /  {max(all_pnls):+.2f}%")
        # Skew approx (Pearson median-mode-ish)
        s = (3 * (statistics.mean(all_pnls) - statistics.median(all_pnls))) / statistics.stdev(all_pnls)
        print(f"  skew (pearson) : {s:+.3f}   (positive → right tail; means losers cluster, winners big)")

    # 5. Kelly fraction estimate per surviving combo — sizing implication
    print()
    print(_color("═" * 90, "36"))
    print(_color("  KELLY SIZING for surviving combos (5d, n>=30, avg>0)", "1;36"))
    print(_color("═" * 90, "36"))
    pool: dict[tuple[str, str], list[float]] = defaultdict(list)
    for sym_res in data:
        for f in sym_res.get("folds") or []:
            for t in f.get("trades") or []:
                if "pnl_5d" in t and not t.get("neutral_5d"):
                    pool[(t.get("signal_type", "?"), t.get("direction", "?"))].append(t["pnl_5d"])
    print(f"  {'COMBO':<40}{'N':>6}{'WR':>8}{'AVG':>10}{'WIN_AVG':>11}{'LOSS_AVG':>11}{'KELLY':>10}")
    print("─" * 96)
    rows = []
    for k, pnls in pool.items():
        if len(pnls) < 30:
            continue
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        if not wins or not losses:
            continue
        p = len(wins) / len(pnls)
        b = statistics.mean(wins) / abs(statistics.mean(losses))
        kelly = (p * (b + 1) - 1) / b   # Kelly fraction of bankroll
        rows.append((k, len(pnls), p * 100, statistics.mean(pnls),
                     statistics.mean(wins), statistics.mean(losses), kelly))
    rows.sort(key=lambda r: r[-1], reverse=True)
    for k, n, wr, avg, wavg, lavg, kelly in rows:
        col = "32" if kelly > 0 else "31"
        kelly_str = _color(f"{kelly*100:>6.1f}%", col)
        print(f"  {(k[0]+'/'+k[1])[:39]:<40}{n:>6}{wr:>7.1f}%{avg:>9.2f}%{wavg:>10.2f}%{lavg:>10.2f}%  {kelly_str}")
    print()
    print(_color("  Kelly > 0 → +EV (size bet at that fraction).  Kelly < 0 → mute.", "90"))
    print(_color("  Practical: cap actual sizing at ¼-Kelly to survive vol.", "90"))

if __name__ == "__main__":
    sys.exit(main() or 0)
