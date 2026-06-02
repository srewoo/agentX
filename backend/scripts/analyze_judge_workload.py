#!/usr/bin/env python3
"""How many trades would the LLM judge have to actually drop to make the
system profitable?

For each (symbol, signal_type, direction) tuple, compute the marginal
contribution to universe P&L. Sort by negative contribution → those are
the trades the LLM judge MUST veto for the strategy to work.
"""
from __future__ import annotations
import argparse
import json
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

    # Build (symbol, type, dir) → pnls
    bucket: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for sym_res in data:
        sym = sym_res.get("symbol")
        for f in sym_res.get("folds") or []:
            for t in f.get("trades") or []:
                if "pnl_5d" not in t or t.get("neutral_5d"):
                    continue
                bucket[(sym, t.get("signal_type", "?"), t.get("direction", "?"))].append(t["pnl_5d"])

    # Compute total P&L contribution per bucket = sum of pnl/100 (treating
    # each trade as a fixed-notional bet, equal-weighted universe)
    rows = []
    total_pnl_sum = 0.0
    total_trades = 0
    for k, pnls in bucket.items():
        s = sum(pnls)
        total_pnl_sum += s
        total_trades += len(pnls)
        rows.append((k, len(pnls), statistics.mean(pnls), s))
    # Worst contributors first
    rows.sort(key=lambda r: r[3])

    print(_color("═" * 95, "36"))
    print(_color(f"  WORST TRADE BUCKETS — judge must veto these for the system to be +EV", "1;36"))
    print(_color(f"  Baseline: total P&L sum = {total_pnl_sum:+.1f} percentage-points over {total_trades} trades", "36"))
    print(_color("═" * 95, "36"))
    print(f"  {'SYMBOL':<14}{'SIGNAL_TYPE':<24}{'DIR':<10}{'N':>4}{'AVG':>10}{'SUM_PNL':>10}{'CUMULATIVE':>14}")
    print("─" * 95)
    cum = 0.0
    cum_cleaned = total_pnl_sum
    target_row = None
    for i, (k, n, avg, s) in enumerate(rows[:25]):
        cum += s
        cum_cleaned -= s
        col = "31" if s < 0 else "32"
        # If we muted everything from rows[0..i+1], what would universe sum be?
        print(f"  {k[0]:<14}{k[1][:23]:<24}{k[2]:<10}{n:>4}"
              f"{_color(f'{avg:+5.2f}%', col):>19}"
              f"{_color(f'{s:+6.1f}', col):>19}"
              f"{_color(f'{cum_cleaned:+6.1f}', '32' if cum_cleaned > 0 else '31'):>23}")
        if target_row is None and cum_cleaned > 0:
            target_row = i + 1
    print("─" * 95)
    if target_row is not None:
        print(_color(f"  → Vetoing the top {target_row} worst buckets flips the universe to +EV.", "1;32"))
    else:
        print(_color("  → Even vetoing the top 25 worst buckets isn't enough — strategy fundamentally broken on this universe.", "1;31"))

    # The reverse: which buckets MUST survive?
    print()
    print(_color("═" * 95, "36"))
    print(_color("  BEST TRADE BUCKETS — these are what the engine should keep firing", "1;36"))
    print(_color("═" * 95, "36"))
    print(f"  {'SYMBOL':<14}{'SIGNAL_TYPE':<24}{'DIR':<10}{'N':>4}{'AVG':>10}{'SUM_PNL':>10}")
    print("─" * 75)
    for k, n, avg, s in sorted(rows, key=lambda r: -r[3])[:15]:
        col = "32" if s > 0 else "31"
        print(f"  {k[0]:<14}{k[1][:23]:<24}{k[2]:<10}{n:>4}"
              f"{_color(f'{avg:+5.2f}%', col):>19}"
              f"{_color(f'{s:+6.1f}', col):>19}")

    # Best & worst single trade (sanity)
    all_trades = []
    for sym_res in data:
        for f in sym_res.get("folds") or []:
            for t in f.get("trades") or []:
                if "pnl_5d" in t and not t.get("neutral_5d"):
                    all_trades.append((sym_res.get("symbol"), t))
    all_trades.sort(key=lambda r: r[1]["pnl_5d"])
    print()
    print(_color("  WORST 5 INDIVIDUAL TRADES", "1;31"))
    for sym, t in all_trades[:5]:
        print(f"    {sym:<10} {t.get('signal_type'):<22} {t.get('direction'):<10} bar={t.get('bar_index'):<5} pnl={t['pnl_5d']:+.2f}%")
    print(_color("  BEST 5 INDIVIDUAL TRADES", "1;32"))
    for sym, t in all_trades[-5:]:
        print(f"    {sym:<10} {t.get('signal_type'):<22} {t.get('direction'):<10} bar={t.get('bar_index'):<5} pnl={t['pnl_5d']:+.2f}%")


if __name__ == "__main__":
    sys.exit(main() or 0)
