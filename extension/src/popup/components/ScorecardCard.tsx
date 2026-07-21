import { useEffect, useState } from "react";
import { api } from "../../shared/api";
import type { Scorecard } from "../../shared/types";

/**
 * North-star scorecard — the ONE headline every decision should key off:
 * cost-adjusted, benchmark-EXCESS expectancy per trade + its 95% lower bound
 * (is the edge above zero with confidence?), calibration Brier (can we trust
 * the probabilities?), and forward-trade progress toward the 300-trade proof
 * bar. Win rate is deliberately a small supporting stat, not the headline.
 */

const VERDICT: Record<Scorecard["verdict"], { label: string; cls: string }> = {
  PROVEN: { label: "PROVEN EDGE", cls: "text-profit border-profit/40 bg-profit/10" },
  SIGNIFICANT_BUT_UNDER_SAMPLE: { label: "SIGNIFICANT · UNDER-SAMPLED", cls: "text-amber-300 border-amber-500/40 bg-amber-500/10" },
  PROMISING: { label: "PROMISING · UNPROVEN", cls: "text-amber-300 border-amber-500/40 bg-amber-500/10" },
  NO_EDGE_YET: { label: "NO EDGE YET", cls: "text-loss border-loss/40 bg-loss/10" },
};

function pct(v: number | null | undefined, dp = 3): string {
  if (v == null) return "—";
  return `${v >= 0 ? "+" : ""}${v.toFixed(dp)}%`;
}

export default function ScorecardCard() {
  const [sc, setSc] = useState<Scorecard | null>(null);

  useEffect(() => {
    api.getScorecard().then((r) => setSc(r.data)).catch(() => setSc(null));
  }, []);

  if (!sc) return null;

  const v = VERDICT[sc.verdict] ?? VERDICT.NO_EDGE_YET;
  const exp = sc.excess_expectancy_pct;
  const lb = sc.excess_expectancy_lb95_pct;
  const expClass = exp == null ? "text-zinc-400" : exp > 0 ? "text-profit" : "text-loss";
  const lbClass = lb == null ? "text-zinc-500" : lb > 0 ? "text-profit" : "text-loss";

  return (
    <div className="border-b border-border bg-zinc-900/50 px-3 py-2">
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-[10px] uppercase tracking-wider text-zinc-400">
          🎯 North Star — Excess Expectancy / Trade
        </span>
        <span className={`text-[9px] font-semibold uppercase px-1.5 py-0.5 rounded-full border ${v.cls}`}>
          {v.label}
        </span>
      </div>

      <div className="flex items-baseline gap-3">
        <span className={`text-2xl font-bold ${expClass}`} title="Mean per-trade P&L in excess of NIFTY, net of costs">
          {pct(exp)}
        </span>
        <span className={`text-[11px] ${lbClass}`} title="95% lower confidence bound — the edge is only PROVEN when this is > 0">
          95% LB {pct(lb)}
        </span>
        {sc.benchmark_symbol && (
          <span className="text-[10px] text-zinc-500">vs {sc.benchmark_symbol}</span>
        )}
      </div>

      {/* Forward-trade progress toward the 300-trade proof bar. */}
      <div className="mt-2">
        <div className="flex items-center justify-between text-[10px] text-zinc-500 mb-0.5">
          <span>Forward trades (proof bar)</span>
          <span className="text-zinc-400">{sc.forward_trades} / {sc.target_trades}</span>
        </div>
        <div className="h-1.5 rounded-full bg-zinc-800 overflow-hidden">
          <div
            className="h-full bg-brand"
            style={{ width: `${Math.max(2, sc.progress_pct)}%` }}
          />
        </div>
      </div>

      {/* Supporting stats — deliberately small; NOT the objective. */}
      <div className="flex items-center gap-3 mt-2 text-[10px] text-zinc-500 flex-wrap">
        <span title="Calibration: predicted vs realized. Lower is better (0 = perfect).">
          Brier <span className="text-zinc-300">{sc.brier == null ? "—" : sc.brier.toFixed(3)}</span>
        </span>
        <span className="text-zinc-700">·</span>
        <span title="Supporting stat only — not the objective">
          WR <span className="text-zinc-400">{sc.win_rate == null ? "—" : `${(sc.win_rate * 100).toFixed(1)}%`}</span>
        </span>
        <span className="text-zinc-700">·</span>
        <span>Sharpe/trade <span className="text-zinc-400">{sc.sharpe_per_trade ?? "—"}</span></span>
        <span className="text-zinc-700">·</span>
        <span>Max DD <span className="text-zinc-400">{sc.max_drawdown_pct == null ? "—" : `${sc.max_drawdown_pct}%`}</span></span>
      </div>
    </div>
  );
}
