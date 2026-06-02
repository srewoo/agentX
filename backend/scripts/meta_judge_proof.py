#!/usr/bin/env python3
"""The moneymaker proof — does the meta-judge actually flip the system to +EV?

Loads a saved walk_fwd_*.json (raw deterministic-engine trades), trains the
MetaJudge classifier on the first K folds, evaluates on the held-out fold K.

This is the honest, falsifiable claim: "the architectural bet (filtering
makes the system profitable) is testable, and here's the result."
"""
from __future__ import annotations
import argparse
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from app.services.meta_judge import MetaJudge, featurise, evaluate  # type: ignore


def _color(s, c): return f"\033[{c}m{s}\033[0m"


def _trade_to_record(t: dict, symbol: str) -> dict:
    """Project the raw walk_fwd trade row into the meta-judge schema."""
    rec = dict(t)
    rec["symbol"] = symbol
    rec["sector"] = "Unknown"  # we don't carry sector through the harness
    rec["win"] = bool(t.get("win_5d"))
    rec["pnl"] = float(t.get("pnl_5d", 0.0))
    # Map orchestrator-style numeric features into model schema. The
    # backtester only exposes `regime` + signal_type + direction directly;
    # the rest fall back to 0.0, which is the model's "no-info" default.
    return rec


def run(path: str, *, target_tpr: float, n_stumps: int, label_mode: str = "pnl_positive") -> int:
    data = json.loads(Path(path).read_text())

    # Bucket trades by fold so we can train on [0..k-1] and test on [k].
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

    n_folds = len(folds)
    if n_folds < 2:
        print(_color("Need at least 2 folds — re-run the harness with --folds 3 or higher.", "31"))
        return 1

    total_trades = sum(len(f) for f in folds)
    print(_color("═" * 90, "36"))
    print(_color("  META-JUDGE PROOF — train on early folds, score on held-out tail", "1;36"))
    print(_color(f"  Source: {Path(path).name}", "36"))
    print(_color(f"  Folds: {n_folds}, total trades: {total_trades}, target TPR: {target_tpr}", "36"))
    print(_color(f"  Stumps per fold: {n_stumps}", "36"))
    print(_color("═" * 90, "36"))
    print()
    print(f"{'TRAIN':>8}{'TEST':>6}{'N_TRAIN':>10}{'N_TEST':>9}{'AUC':>8}{'TPR':>8}{'TNR':>8}"
          f"{'KEPT_WR':>11}{'KEPT_AVG':>11}{'KEPT_SUM':>11}{'VERDICT':>14}")
    print("─" * 90)

    overall_kept_sum = 0.0
    overall_kept_n = 0
    overall_kept_wins = 0

    for k in range(1, n_folds):
        train = [t for kk in range(k) for t in folds[kk]]
        test = folds[k]
        if len(train) < 50 or not test:
            print(f"  {f'0..{k-1}':>8}{k:>6}  insufficient training data")
            continue

        model = MetaJudge.train(train, n_stumps=n_stumps, target_tpr=target_tpr, label_mode=label_mode)
        ev = evaluate(model, test)
        if not ev:
            continue

        kept_avg = ev["kept_avg_pnl"]
        kept_wr = ev["kept_wr_pct"]
        kept_sum = ev["kept_sum_pnl"]

        # Raw test baseline (no model) for comparison.
        raw_avg = statistics.mean(t["pnl"] for t in test)

        # Verdict
        col_avg = "32" if kept_avg > 0 else "31"
        verdict = "+EV" if kept_avg > 0.05 else ("break-even" if kept_avg > -0.05 else "loss")
        v_col = "32" if kept_avg > 0.05 else ("33" if kept_avg > -0.05 else "31")

        print(f"  {f'0..{k-1}':>8}{k:>6}{len(train):>10}{ev['n_kept']+ev['n_dropped']:>9}"
              f"{ev['auc']:>8.3f}{ev['tpr']:>8.2f}{ev['tnr']:>8.2f}"
              f"{_color(f'{kept_wr:>5.1f}%', col_avg):>20}"
              f"{_color(f'{kept_avg:+5.2f}%', col_avg):>20}"
              f"{_color(f'{kept_sum:+6.1f}', col_avg):>20}"
              f"  {_color(verdict, v_col)}")

        overall_kept_sum += kept_sum
        overall_kept_n += ev["n_kept"]
        overall_kept_wins += int(kept_wr / 100.0 * ev["n_kept"])

    print("─" * 90)
    if overall_kept_n > 0:
        overall_avg = overall_kept_sum / overall_kept_n
        overall_wr = overall_kept_wins / overall_kept_n * 100.0
        col = "32" if overall_avg > 0 else "31"
        print()
        print(_color("AGGREGATE OOS PERFORMANCE", "1;36"))
        print(f"  Trades kept   : {overall_kept_n}")
        print(f"  Wins          : {overall_kept_wins}")
        print(f"  Aggregate WR  : {_color(f'{overall_wr:.1f}%', col)}")
        print(f"  Aggregate avg : {_color(f'{overall_avg:+.2f}%', col)} per kept trade")
        print(f"  Aggregate sum : {_color(f'{overall_kept_sum:+.1f}', col)} pp")
        if overall_avg > 0:
            print()
            print(_color("  ✓ META-JUDGE FLIPS THE SYSTEM TO +EV ON HELD-OUT DATA.", "1;32"))
            print(_color("    Production deploy: replace LLM judge with this model.", "32"))
        elif overall_avg > -0.10:
            print(_color("  ~ Break-even. More features (live macro snapshot, options, etc.) needed.", "1;33"))
        else:
            print(_color("  ✗ Meta-judge didn't beat the baseline. Architecture needs rethinking.", "1;31"))
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("path", help="walk_fwd_*.json file")
    p.add_argument("--target-tpr", type=float, default=0.70, dest="target_tpr")
    p.add_argument("--stumps", type=int, default=20)
    p.add_argument("--label-mode", default="pnl_positive",
                   choices=["win", "pnl_positive", "ev_positive"])
    args = p.parse_args()
    return run(args.path, target_tpr=args.target_tpr, n_stumps=args.stumps,
               label_mode=args.label_mode)


if __name__ == "__main__":
    sys.exit(main())
