import { useEffect, useState } from "react";
import { api } from "../../shared/api";
import { getSettings } from "../../shared/storage";
import type { BacktestResult, AppSettings } from "../../shared/types";

interface Props { symbol: string; }

const PERIODS: Array<{ id: "3mo" | "6mo" | "1y" | "2y" | "5y"; label: string }> = [
  { id: "6mo", label: "6M" },
  { id: "1y", label: "1Y" },
  { id: "2y", label: "2Y" },
];

export default function BacktestPanel({ symbol }: Props) {
  const [period, setPeriod] = useState<"3mo" | "6mo" | "1y" | "2y" | "5y">("1y");
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(true);
  const [costPct, setCostPct] = useState<number>(0.5);

  useEffect(() => {
    getSettings().then((s) => {
      const v = (s as Partial<AppSettings>).roundtrip_cost_pct;
      if (typeof v === "number") setCostPct(v);
    });
  }, []);

  const run = async () => {
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const r = await api.backtest(symbol, period);
      setResult(r);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Backtest failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="bg-panel rounded-xl border border-border overflow-hidden">
      <button onClick={() => setCollapsed((v) => !v)} className="w-full flex items-center justify-between px-3 py-2 text-[11px] font-semibold text-zinc-300 hover:bg-zinc-800/50">
        <span>🔁 Backtest signals on {symbol}</span>
        <span className="text-zinc-500">{collapsed ? "▼" : "▲"}</span>
      </button>
      {!collapsed && (
        <div className="px-3 py-2 space-y-2">
          <div className="flex items-center gap-1.5">
            <span className="text-[10px] text-zinc-500">Period</span>
            {PERIODS.map((p) => (
              <button key={p.id} onClick={() => setPeriod(p.id)}
                className={`text-[10px] px-1.5 py-0.5 rounded border ${period === p.id ? "border-brand/40 bg-brand/15 text-brand-light" : "border-border text-zinc-500 hover:text-zinc-300"}`}>
                {p.label}
              </button>
            ))}
            <button onClick={run} disabled={loading}
              className="ml-auto text-[11px] px-3 py-1 rounded bg-brand text-white hover:bg-brand/80 disabled:opacity-50">
              {loading ? "Running…" : "Run backtest"}
            </button>
          </div>
          {error && <div className="text-[11px] text-loss">{error}</div>}
          {result && (
            <div className="space-y-2">
              <div className="text-[11px] text-zinc-400">
                <span className="font-semibold text-zinc-200">{result.total_signals}</span> historical signals evaluated
              </div>
              {(() => {
                // Backend already nets out methodology.transaction_cost_pct per trade.
                // The user's configured roundtrip_cost_pct is the *total* assumed cost.
                // We only subtract the *additional* slippage on top of what's already netted.
                const backendCost = result.methodology?.transaction_cost_pct ?? 0;
                const additional = Math.max(0, costPct - backendCost);
                return (
                  <>
                    <div className="grid grid-cols-4 gap-1">
                      {Object.entries(result.windows ?? {})
                        .sort(([a], [b]) => Number(a) - Number(b))
                        .map(([days, w]) => {
                          const netPnl = w.avg_pnl_pct - additional;
                          return (
                            <div key={days} className="bg-zinc-900/60 rounded-lg p-1.5 text-center">
                              <div className="text-[10px] text-zinc-500">{days}d</div>
                              <div className={`text-xs font-bold ${w.win_rate >= 50 ? "text-profit" : "text-loss"}`}>
                                {w.win_rate.toFixed(0)}%
                              </div>
                              <div className={`text-[10px] ${w.avg_pnl_pct >= 0 ? "text-profit" : "text-loss"}`}>
                                {w.avg_pnl_pct >= 0 ? "+" : ""}{w.avg_pnl_pct.toFixed(1)}%
                              </div>
                              {additional > 0 && (
                                <div className={`text-[9px] ${netPnl >= 0 ? "text-emerald-400/70" : "text-red-400/70"}`}>
                                  net {netPnl >= 0 ? "+" : ""}{netPnl.toFixed(1)}%
                                </div>
                              )}
                              <div className="text-[9px] text-zinc-600">{w.trades} trades</div>
                            </div>
                          );
                        })}
                    </div>
                    <div className="text-[10px] text-zinc-500 italic leading-relaxed">
                      Walk-forward methodology: at each historical bar, only data ≤ that bar feeds the indicators (no look-ahead).
                      Backend already nets {backendCost.toFixed(2)}% brokerage+STT per round-trip.
                      {additional > 0
                        ? ` "net" subtracts ${additional.toFixed(2)}% additional slippage to reach your configured ${costPct.toFixed(2)}% total.`
                        : ` Your configured ${costPct.toFixed(2)}% target ≤ backend's net cost — no extra subtraction needed.`}
                      {" "}Win rate is gross — small wins flip to losses after costs (real net win rate is typically 5–10pp lower).
                    </div>
                  </>
                );
              })()}
              {result.by_signal_type && result.by_signal_type.length > 0 && (
                <details className="text-[11px]">
                  <summary className="cursor-pointer text-zinc-500 hover:text-zinc-300">By signal type</summary>
                  <div className="mt-1 space-y-0.5">
                    {result.by_signal_type
                      .sort((a, b) => b.win_rate - a.win_rate)
                      .map((row) => (
                        <div key={row.signal_type} className="flex justify-between px-1.5 py-0.5 rounded bg-zinc-900/40">
                          <span className="text-zinc-300 truncate">{row.signal_type}</span>
                          <span className="flex gap-2 flex-shrink-0">
                            <span className={row.win_rate >= 50 ? "text-profit" : "text-loss"}>{row.win_rate.toFixed(0)}%</span>
                            <span className="text-zinc-500">n={row.trades}</span>
                          </span>
                        </div>
                      ))}
                  </div>
                </details>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
