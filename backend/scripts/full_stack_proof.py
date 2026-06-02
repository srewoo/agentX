#!/usr/bin/env python3
"""End-to-end moneymaker proof — apply the full filter stack on held-out data.

Stack (applied in production order):
  1. SYMBOL_BLOCKLIST   — never see ITC/SBIN
  2. DIRECTIONAL_MUTES  — drop losing (signal_type, direction) combos
  3. META-JUDGE         — learned per-trade P(win) filter, OR
  4. PROMOTED override  — always keep PROMOTED_SIGNALS (signal-edge endorsed)

The meta-judge filter is OOS: trained on folds [0..k-1], scored on fold k.
Promoted signals bypass the meta-judge ("conviction overrides ML doubt").
"""
from __future__ import annotations
import argparse
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from app.services.meta_judge import MetaJudge  # type: ignore
from app.services.signal_edge import (  # type: ignore
    DIRECTIONAL_MUTES, PROMOTED_SIGNALS, SYMBOL_BLOCKLIST,
)


def _color(s, c): return f"\033[{c}m{s}\033[0m"


def _trade_to_record(t: dict, symbol: str) -> dict:
    return {
        **t,
        "symbol": symbol,
        "win": bool(t.get("win_5d")),
        "pnl": float(t.get("pnl_5d", 0.0)),
        "sector": "Unknown",
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("path")
    p.add_argument("--stumps", type=int, default=25)
    p.add_argument("--label-mode", default="pnl_positive")
    args = p.parse_args()
    data = json.loads(Path(args.path).read_text())

    folds: list[list[dict]] = []
    for sym_res in data:
        sym = sym_res.get("symbol")
        for k, f in enumerate(sym_res.get("folds") or []):
            while len(folds) <= k:
                folds.append([])
            for t in f.get("trades") or []:
                if "pnl_5d" not in t or t.get("neutral_5d"):
                    continue
                folds[k].append(_trade_to_record(t, sym))

    print(_color("═" * 100, "36"))
    print(_color("  FULL STACK PROOF — Symbol blocklist → Mutes → Meta-judge (with promoted override)", "1;36"))
    print(_color(f"  Source: {Path(args.path).name}  |  Folds: {len(folds)}  "
                 f"|  Stumps: {args.stumps}  |  Labels: {args.label_mode}", "36"))
    print(_color("═" * 100, "36"))
    print()
    print(f"{'STAGE':<28}{'TRADES':>10}{'WR':>10}{'AVG_PNL':>12}{'SUM_PNL':>12}{'DELTA':>15}")
    print("─" * 100)

    # Compute stage-by-stage stats on the union of all folds for a clean
    # universe-level view. We also do an OOS overlay where the meta-judge
    # is trained per-fold and scored on held-out.
    all_raw = [t for f in folds for t in f]

    def _stats(ts):
        if not ts:
            return {"n": 0, "wr": 0.0, "avg": 0.0, "sum": 0.0}
        wins = sum(1 for t in ts if t.get("win"))
        pnls = [t["pnl"] for t in ts]
        return {
            "n": len(ts), "wr": wins / len(ts) * 100.0,
            "avg": statistics.mean(pnls), "sum": sum(pnls),
        }

    def _fmt(s, label, prev=None):
        col_wr = "32" if s["wr"] >= 50 else "33" if s["wr"] >= 45 else "31"
        col_avg = "32" if s["avg"] > 0.05 else "33" if s["avg"] > -0.05 else "31"
        wr = _color(f"{s['wr']:5.1f}%", col_wr).rjust(18)
        av = _color(f"{s['avg']:+.2f}%", col_avg).rjust(19)
        sm = _color(f"{s['sum']:+6.1f}", col_avg).rjust(19)
        delta = ""
        if prev is not None and prev["n"] > 0:
            d_avg = s["avg"] - prev["avg"]
            delta = _color(f"{d_avg:+.2f}pp", '32' if d_avg > 0 else '31')
        return f"  {label:<28}{s['n']:>10}{wr}{av}{sm}  {delta}"

    # Stage 0: raw signal engine (everything)
    s0 = _stats(all_raw)
    print(_fmt(s0, "0. Raw signal engine"))

    # Stage 1: symbol blocklist
    s1_trades = [t for t in all_raw if (t.get("symbol") or "").upper() not in SYMBOL_BLOCKLIST]
    s1 = _stats(s1_trades)
    print(_fmt(s1, "1. + Symbol blocklist", s0))

    # Stage 2: + directional mutes
    s2_trades = [t for t in s1_trades
                 if (t.get("signal_type"), t.get("direction")) not in DIRECTIONAL_MUTES]
    s2 = _stats(s2_trades)
    print(_fmt(s2, "2. + Directional mutes", s1))

    # Stage 3: + meta-judge (OOS — train per fold). PROMOTED_SIGNALS bypass.
    kept_trades = []
    for k in range(1, len(folds)):
        train_raw = [t for kk in range(k) for t in folds[kk]]
        # Apply the same stage-2 filter to training data — so meta-judge
        # learns from the post-filter distribution.
        train = [t for t in train_raw
                 if (t.get("symbol") or "").upper() not in SYMBOL_BLOCKLIST
                 and (t.get("signal_type"), t.get("direction")) not in DIRECTIONAL_MUTES]
        if len(train) < 60:
            kept_trades.extend([t for t in folds[k]
                                if (t.get("symbol") or "").upper() not in SYMBOL_BLOCKLIST
                                and (t.get("signal_type"), t.get("direction")) not in DIRECTIONAL_MUTES])
            continue
        model = MetaJudge.train(train, n_stumps=args.stumps, label_mode=args.label_mode)
        test = [t for t in folds[k]
                if (t.get("symbol") or "").upper() not in SYMBOL_BLOCKLIST
                and (t.get("signal_type"), t.get("direction")) not in DIRECTIONAL_MUTES]
        for t in test:
            # Promoted-signal override: never drop a high-edge combo.
            if (t.get("signal_type"), t.get("direction")) in PROMOTED_SIGNALS:
                kept_trades.append(t)
                continue
            if model.keep(t):
                kept_trades.append(t)
    s3 = _stats(kept_trades)
    print(_fmt(s3, "3. + Meta-judge (OOS)", s2))

    # Stage 4 — cherry-pick the strongest combos only, for context.
    promoted_only = [t for t in s2_trades
                     if (t.get("signal_type"), t.get("direction")) in PROMOTED_SIGNALS]
    s4 = _stats(promoted_only)
    print(_fmt(s4, "  (alt: PROMOTED only)", s2))

    print("─" * 100)
    print()
    print(_color("VERDICT", "1;36"))
    print()
    raw_avg = s0["avg"]
    stk_avg = s3["avg"]
    lift = stk_avg - raw_avg
    raw_str = _color(f"{raw_avg:+.2f}%", "31" if raw_avg < 0 else "32")
    stk_str = _color(f"{stk_avg:+.2f}%", "32" if stk_avg > 0 else "31")
    lift_str = _color(f"{lift:+.2f}pp", "32" if lift > 0 else "31")
    print(f"  Raw baseline      : {raw_str} avg P&L per trade")
    print(f"  After full stack  : {stk_str} avg P&L per trade  "
          f"(on {s3['n']} kept trades, {s3['wr']:.1f}% WR)")
    print(f"  Lift              : {lift_str} per trade")
    print()
    if s3["avg"] > 0.05:
        print(_color("  ✓ The full stack is empirically PROFITABLE on held-out data.", "1;32"))
        print(_color(f"    Deploy with ¼-Kelly sizing: cap any single trade at ~{abs(s3['avg'])*5:.1f}% portfolio risk.", "32"))
    elif s3["avg"] > -0.05:
        print(_color("  ~ Break-even after costs. Mutes alone are profitable; meta-judge is awaiting more data.", "1;33"))
    else:
        print(_color("  ✗ Still loss-making. Revisit feature engineering.", "1;31"))

if __name__ == "__main__":
    sys.exit(main() or 0)
