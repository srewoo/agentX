import { useEffect, useMemo, useState } from "react";
import { api } from "../../shared/api";
import type { PerformanceByTypeRow, BacktestRun } from "../../shared/types";

/**
 * Performance — read-only view over the orchestrator's self-tracked outcomes.
 *
 * Sources:
 *  - `/api/performance/by-type`     — live tracked outcomes per (signal_type, direction)
 *  - `/api/performance/backtest-history` — last N weekly autonomous backtest runs
 *
 * No mutations. No LLM calls. Pure summary.
 */

function fmtPct(n: number | null | undefined, digits = 1): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `${n >= 0 ? "+" : ""}${n.toFixed(digits)}%`;
}

function fmtWin(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `${n.toFixed(1)}%`;
}

function dateOnly(iso: string): string {
  try { return new Date(iso).toLocaleDateString(); } catch { return iso.slice(0, 10); }
}

export default function Performance() {
  const [rows, setRows] = useState<PerformanceByTypeRow[]>([]);
  const [runs, setRuns] = useState<BacktestRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [byType, history] = await Promise.all([
          api.getPerformanceByType(),
          api.getBacktestHistory(12),
        ]);
        if (!alive) return;
        setRows(byType.data || []);
        setRuns(history.runs || []);
      } catch (e) {
        if (!alive) return;
        setError(e instanceof Error ? e.message : "Failed to load performance");
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, []);

  const sorted = useMemo(
    // Best-edge first: sort by win_rate, tie-break by sample size.
    () => [...rows].sort((a, b) => (b.win_rate - a.win_rate) || (b.total_signals - a.total_signals)),
    [rows]
  );

  const totals = useMemo(() => {
    const total = rows.reduce((s, r) => s + r.total_signals, 0);
    const wins = rows.reduce((s, r) => s + r.wins, 0);
    return {
      total,
      wins,
      win_rate: total > 0 ? (wins / total) * 100 : null,
    };
  }, [rows]);

  if (loading) {
    return <div className="p-3 text-xs text-zinc-500">Loading performance…</div>;
  }
  if (error) {
    return <div className="p-3 text-xs text-red-400">{error}</div>;
  }

  return (
    <div className="overflow-y-auto h-full p-3 space-y-4 text-zinc-200">
      <div>
        <h3 className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-1.5">
          Live tracked outcomes
        </h3>
        {rows.length === 0 ? (
          <p className="text-[11px] text-zinc-500">
            No resolved signals yet. Performance fills in as past signals reach their evaluation window.
          </p>
        ) : (
          <>
            <div className="text-[11px] text-zinc-400 mb-2">
              <span className="text-zinc-200 font-semibold">{totals.total}</span> evaluated ·{" "}
              <span className="text-zinc-200 font-semibold">{fmtWin(totals.win_rate)}</span> overall win rate
            </div>
            <div className="border border-border rounded-md overflow-hidden">
              <table className="w-full text-[11px]">
                <thead className="bg-zinc-900/60 text-zinc-500">
                  <tr>
                    <th className="text-left px-2 py-1 font-medium">Signal</th>
                    <th className="text-left px-2 py-1 font-medium">Dir</th>
                    <th className="text-right px-2 py-1 font-medium">N</th>
                    <th className="text-right px-2 py-1 font-medium">Win %</th>
                    <th className="text-right px-2 py-1 font-medium">Avg PnL</th>
                  </tr>
                </thead>
                <tbody>
                  {sorted.map((r) => (
                    <tr key={`${r.signal_type}:${r.direction}:${r.timeframe ?? ""}`}
                        className="border-t border-border/60">
                      <td className="px-2 py-1 text-zinc-200">{r.signal_type}</td>
                      <td className={`px-2 py-1 capitalize ${
                        r.direction === "bullish" ? "text-emerald-400"
                          : r.direction === "bearish" ? "text-red-400"
                          : "text-zinc-400"}`}>
                        {r.direction}
                      </td>
                      <td className="px-2 py-1 text-right text-zinc-400">{r.total_signals}</td>
                      <td className={`px-2 py-1 text-right font-medium ${
                        r.win_rate >= 55 ? "text-emerald-400"
                          : r.win_rate >= 45 ? "text-zinc-300"
                          : "text-red-400"}`}>
                        {fmtWin(r.win_rate)}
                      </td>
                      <td className={`px-2 py-1 text-right font-medium ${
                        r.avg_pnl_pct > 0 ? "text-emerald-400"
                          : r.avg_pnl_pct < 0 ? "text-red-400"
                          : "text-zinc-400"}`}>
                        {fmtPct(r.avg_pnl_pct)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>

      <div>
        <h3 className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-1.5">
          Recent backtest runs
        </h3>
        {runs.length === 0 ? (
          <p className="text-[11px] text-zinc-500">
            No backtests yet. The autonomous weekly backtest will populate this after its first run.
          </p>
        ) : (
          <div className="border border-border rounded-md overflow-hidden">
            <table className="w-full text-[11px]">
              <thead className="bg-zinc-900/60 text-zinc-500">
                <tr>
                  <th className="text-left px-2 py-1 font-medium">Run</th>
                  <th className="text-right px-2 py-1 font-medium">Stocks</th>
                  <th className="text-right px-2 py-1 font-medium">Signals</th>
                  <th className="text-right px-2 py-1 font-medium">Win %</th>
                  <th className="text-right px-2 py-1 font-medium">Avg PnL</th>
                  <th className="text-left px-2 py-1 font-medium">Best</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.id} className="border-t border-border/60">
                    <td className="px-2 py-1 text-zinc-300">{dateOnly(r.run_at)}</td>
                    <td className="px-2 py-1 text-right text-zinc-400">{r.stocks_count}</td>
                    <td className="px-2 py-1 text-right text-zinc-400">{r.total_signals}</td>
                    <td className="px-2 py-1 text-right text-zinc-200">{fmtWin(r.directional_win_rate)}</td>
                    <td className={`px-2 py-1 text-right ${
                      (r.avg_pnl_pct ?? 0) > 0 ? "text-emerald-400"
                        : (r.avg_pnl_pct ?? 0) < 0 ? "text-red-400"
                        : "text-zinc-400"}`}>
                      {fmtPct(r.avg_pnl_pct)}
                    </td>
                    <td className="px-2 py-1 text-zinc-400 truncate max-w-[120px]">
                      {r.best_signal_type ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
