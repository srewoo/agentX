#!/usr/bin/env python3
"""Honest out-of-sample ceiling analysis on saved walk_fwd_*.json.

The earlier 'ceiling' picked winning (signal_type, direction) combos using
the same trades it scored — pure in-sample overfit. The honest test:

  • TRAIN window: folds [0 .. k-1]  →  learn which combos are profitable.
  • TEST  window: fold [k]          →  score those combos on unseen data.

Reports for k = 1..N-1:
  • In-sample WR / avg P&L on TRAIN
  • Out-of-sample WR / avg P&L on TEST
  • IS-OOS gap (the overfit penalty)

Also runs a per-symbol allowlist ceiling: train symbol allowlist on TRAIN,
test on TEST. This is what the production cohort dashboard would actually do
when promoting/muting based on recent data.
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


def _wlb(wins: int, n: int) -> float:
    if n <= 0:
        return 0.0
    z = 1.96
    p = wins / n
    denom = 1 + z*z/n
    centre = p + z*z/(2*n)
    margin = z * math.sqrt(max(0.0, (p*(1-p) + z*z/(4*n)) / n))
    return max(0.0, (centre - margin) / denom) * 100.0


def _stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "wr": 0.0, "avg": 0.0, "wlb": 0.0, "sum": 0.0}
    wins = sum(1 for t in trades if t.get("win_5d"))
    pnls = [t["pnl_5d"] for t in trades]
    return {
        "n": len(trades),
        "wr": wins / len(trades) * 100.0,
        "avg": statistics.mean(pnls),
        "wlb": _wlb(wins, len(trades)),
        "sum": sum(pnls),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("path")
    p.add_argument("--min-n", type=int, default=15,
                   help="min trades in TRAIN to consider a combo learnable")
    args = p.parse_args()
    data = json.loads(Path(args.path).read_text())

    # Bucket every trade into (fold_idx, symbol, signal_type, direction).
    # Each input file already segments folds.
    folds: list[list[dict]] = []  # folds[k] = list of trades
    for sym_res in data:
        sym = sym_res.get("symbol")
        for k, f in enumerate(sym_res.get("folds") or []):
            while len(folds) <= k:
                folds.append([])
            for t in f.get("trades") or []:
                if "pnl_5d" not in t or t.get("neutral_5d"):
                    continue
                # Attach symbol so we can build symbol allowlists.
                t = {**t, "_symbol": sym}
                folds[k].append(t)

    n_folds = len(folds)
    if n_folds < 2:
        print(_color("Need at least 2 folds for OOS analysis", "31"))
        return 1

    print(_color("═" * 95, "36"))
    print(_color(f"  HONEST OUT-OF-SAMPLE CEILING — train k folds, test on fold k", "1;36"))
    print(_color(f"  Data: {n_folds} folds, "
                 f"{sum(len(f) for f in folds)} trades, min_n_train={args.min_n}", "36"))
    print(_color("═" * 95, "36"))

    # ── A. Combo-level OOS (train signal_type×direction allowlist) ────────
    print()
    print(_color("A. COMBO ALLOWLIST (signal_type × direction)", "1;36"))
    print(_color(f"{'TRAIN':<8}{'TEST':<8}{'TRAIN_WR':>10}{'TEST_WR':>10}{'TEST_AVG':>11}"
                 f"{'KEPT':>8}{'TEST_N':>10}{'GAP':>10}", "36"))
    print("─" * 95)
    for k in range(1, n_folds):
        train = [t for kk in range(k) for t in folds[kk]]
        test = folds[k]

        # Learn allowlist: keep combo if TRAIN sample ≥ min_n and avg > 0.
        by_combo_train: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for t in train:
            by_combo_train[(t.get("signal_type", "?"), t.get("direction", "?"))].append(t)
        allowlist = {
            k2 for k2, ts in by_combo_train.items()
            if len(ts) >= args.min_n and statistics.mean(t2["pnl_5d"] for t2 in ts) > 0
        }
        if not allowlist:
            print(f"  0..{k-1:<4}{k:<6}  (no positive-avg combos in train)")
            continue

        train_kept = [t for t in train
                      if (t.get("signal_type"), t.get("direction")) in allowlist]
        test_kept = [t for t in test
                     if (t.get("signal_type"), t.get("direction")) in allowlist]
        train_s = _stats(train_kept)
        test_s = _stats(test_kept)
        gap = train_s["wr"] - test_s["wr"]
        col_test_wr = ("32" if test_s["wr"] >= 55 else
                       "33" if test_s["wr"] >= 45 else "31")
        col_avg = "32" if test_s["avg"] > 0 else "31"
        test_wr_str = _color(f'{test_s["wr"]:>5.1f}%', col_test_wr).rjust(18)
        test_avg_str = _color(f'{test_s["avg"]:+.2f}%', col_avg).rjust(19)
        gap_str = _color(f'{gap:+5.1f}pp', '90').rjust(17)
        print(f"  0..{k-1:<4}{k:<6}"
              f"{train_s['wr']:>9.1f}%"
              f"{test_wr_str}{test_avg_str}"
              f"{len(allowlist):>7}{test_s['n']:>10}{gap_str}")

    # ── B. Symbol allowlist OOS ───────────────────────────────────────────
    print()
    print(_color("B. SYMBOL ALLOWLIST (keep symbols with avg P&L > 0 on TRAIN)", "1;36"))
    print(_color(f"{'TRAIN':<8}{'TEST':<8}{'TRAIN_WR':>10}{'TEST_WR':>10}{'TEST_AVG':>11}"
                 f"{'KEPT':>8}{'TEST_N':>10}{'GAP':>10}", "36"))
    print("─" * 95)
    for k in range(1, n_folds):
        train = [t for kk in range(k) for t in folds[kk]]
        test = folds[k]
        by_sym_train: dict[str, list[dict]] = defaultdict(list)
        for t in train:
            by_sym_train[t.get("_symbol", "?")].append(t)
        sym_allow = {s for s, ts in by_sym_train.items()
                     if len(ts) >= args.min_n and statistics.mean(t["pnl_5d"] for t in ts) > 0}
        train_kept = [t for t in train if t.get("_symbol") in sym_allow]
        test_kept = [t for t in test if t.get("_symbol") in sym_allow]
        train_s = _stats(train_kept)
        test_s = _stats(test_kept)
        gap = train_s["wr"] - test_s["wr"]
        col_test_wr = ("32" if test_s["wr"] >= 55 else
                       "33" if test_s["wr"] >= 45 else "31")
        col_avg = "32" if test_s["avg"] > 0 else "31"
        test_wr_str = _color(f'{test_s["wr"]:>5.1f}%', col_test_wr).rjust(18)
        test_avg_str = _color(f'{test_s["avg"]:+.2f}%', col_avg).rjust(19)
        gap_str = _color(f'{gap:+5.1f}pp', '90').rjust(17)
        print(f"  0..{k-1:<4}{k:<6}"
              f"{train_s['wr']:>9.1f}%"
              f"{test_wr_str}{test_avg_str}"
              f"{len(sym_allow):>7}{test_s['n']:>10}{gap_str}"
              f"  {sorted(sym_allow)}")

    # ── C. Combined: symbol × combo allowlist ─────────────────────────────
    print()
    print(_color("C. COMBINED (symbol AND combo allowlist)", "1;36"))
    print(_color(f"{'TRAIN':<8}{'TEST':<8}{'TRAIN_WR':>10}{'TEST_WR':>10}{'TEST_AVG':>11}"
                 f"{'COMBOS':>9}{'SYMS':>7}{'TEST_N':>10}", "36"))
    print("─" * 95)
    for k in range(1, n_folds):
        train = [t for kk in range(k) for t in folds[kk]]
        test = folds[k]
        # Per (symbol, combo) tuple allowlist.
        by_tuple: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for t in train:
            by_tuple[(t.get("_symbol"), t.get("signal_type"), t.get("direction"))].append(t)
        tuple_allow = {k3 for k3, ts in by_tuple.items()
                       if len(ts) >= max(8, args.min_n // 2)
                       and statistics.mean(t["pnl_5d"] for t in ts) > 0}
        train_kept = [t for t in train if (t.get("_symbol"), t.get("signal_type"), t.get("direction")) in tuple_allow]
        test_kept = [t for t in test if (t.get("_symbol"), t.get("signal_type"), t.get("direction")) in tuple_allow]
        train_s = _stats(train_kept)
        test_s = _stats(test_kept)
        col_test_wr = ("32" if test_s["wr"] >= 55 else
                       "33" if test_s["wr"] >= 45 else "31")
        col_avg = "32" if test_s["avg"] > 0 else "31"
        test_wr_str = _color(f'{test_s["wr"]:>5.1f}%', col_test_wr).rjust(18)
        test_avg_str = _color(f'{test_s["avg"]:+.2f}%', col_avg).rjust(19)
        n_unique_combos = len({(s,d) for (_,s,d) in tuple_allow})
        n_unique_syms = len({sy for (sy,_,_) in tuple_allow})
        print(f"  0..{k-1:<4}{k:<6}"
              f"{train_s['wr']:>9.1f}%"
              f"{test_wr_str}{test_avg_str}"
              f"{n_unique_combos:>9}{n_unique_syms:>7}{test_s['n']:>10}")

    # ── D. The "always-train" baseline — what production would actually do.
    # Use ALL folds except the last as training; test on the last fold.
    # This is the realistic deployment scenario.
    print()
    print(_color("D. PRODUCTION SIMULATION — train on all-but-last, test on last", "1;36"))
    print(_color("─" * 95, "36"))
    if n_folds >= 2:
        train = [t for kk in range(n_folds - 1) for t in folds[kk]]
        test = folds[-1]

        # Method 1: Combo allowlist.
        by_combo_train = defaultdict(list)
        for t in train:
            by_combo_train[(t.get("signal_type"), t.get("direction"))].append(t)
        allow1 = {k2 for k2, ts in by_combo_train.items()
                  if len(ts) >= args.min_n and statistics.mean(t["pnl_5d"] for t in ts) > 0}
        test_combo = [t for t in test if (t.get("signal_type"), t.get("direction")) in allow1]
        s_combo = _stats(test_combo)

        # Method 2: Symbol+combo tuple allowlist.
        by_tup_train = defaultdict(list)
        for t in train:
            by_tup_train[(t.get("_symbol"), t.get("signal_type"), t.get("direction"))].append(t)
        allow2 = {k3 for k3, ts in by_tup_train.items()
                  if len(ts) >= max(8, args.min_n // 2)
                  and statistics.mean(t["pnl_5d"] for t in ts) > 0}
        test_tup = [t for t in test if (t.get("_symbol"), t.get("signal_type"), t.get("direction")) in allow2]
        s_tup = _stats(test_tup)

        # Method 3: Raw — no filter.
        s_raw = _stats(test)

        print(f"  {'Strategy':<40}{'n':>6}{'WR':>10}{'WLB':>10}{'AVG':>11}{'SUM':>10}")
        print("─" * 95)
        for label, s in [
            ("Raw (no filter)", s_raw),
            (f"Combo allowlist  ({len(allow1)} combos)", s_combo),
            (f"Symbol×combo tuple  ({len(allow2)} tuples)", s_tup),
        ]:
            col_wr = "32" if s["wr"] >= 55 else "33" if s["wr"] >= 45 else "31"
            col_avg = "32" if s["avg"] > 0 else "31"
            wr_str = _color(f'{s["wr"]:5.1f}%', col_wr)
            avg_str = _color(f'{s["avg"]:+.2f}%', col_avg)
            sum_str = _color(f'{s["sum"]:+6.1f}', col_avg)
            print(f"  {label:<40}{s['n']:>6}{wr_str:>19}{s['wlb']:>9.1f}%"
                  f"{avg_str:>20}{sum_str:>19}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
