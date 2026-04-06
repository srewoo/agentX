import { useEffect, useState, useMemo } from "react";
import SignalCard from "../components/SignalCard";
import { Disclaimer } from "../components/Disclaimer";
import { useSignals } from "../hooks/useSignals";
import { api } from "../../shared/api";
import { DIRECTION_ACTION, getSignalTimeframe } from "../../shared/constants";

type ActionFilter = "ALL" | "BUY" | "SELL" | "HOLD";
type TimeframeFilter = "ALL" | "Intraday" | "Swing" | "Long-term";

interface PerformanceSummary {
  total_evaluated: number;
  total_wins: number;
  win_rate: number;
  avg_pnl_pct: number;
}

interface MarketContext {
  fii_dii: { fii_net: number | null; dii_net: number | null; sentiment: string } | null;
  india_vix: number | null;
  market_regime: { regime: string; confidence: number; description: string } | null;
}

const REGIME_STYLES: Record<string, string> = {
  "Strong Bull": "bg-emerald-500/15 text-emerald-400 border-emerald-500/30",
  "Weak Bull": "bg-emerald-500/10 text-emerald-300 border-emerald-500/20",
  "Strong Bear": "bg-red-500/15 text-red-400 border-red-500/30",
  "Weak Bear": "bg-red-500/10 text-red-300 border-red-500/20",
  "Ranging": "bg-yellow-500/15 text-yellow-400 border-yellow-500/30",
  "Volatile": "bg-orange-500/15 text-orange-400 border-orange-500/30",
};

export default function Dashboard() {
  const { signals, loading, error, unreadCount, markRead, markAllRead, dismiss, reload } = useSignals();
  const [scanning, setScanning] = useState(false);
  const [marketOpen, setMarketOpen] = useState<boolean | null>(null);
  const [perfSummary, setPerfSummary] = useState<PerformanceSummary | null>(null);
  const [marketCtx, setMarketCtx] = useState<MarketContext | null>(null);
  const [indices, setIndices] = useState<Record<string, { price: number; change: number; change_pct: number }> | null>(null);
  const [actionFilter, setActionFilter] = useState<ActionFilter>("ALL");
  const [timeframeFilter, setTimeframeFilter] = useState<TimeframeFilter>("ALL");
  const [cleared, setCleared] = useState(false);

  const filteredSignals = useMemo(() => {
    return signals.filter((s) => {
      if (actionFilter !== "ALL") {
        const action = DIRECTION_ACTION[s.direction] || "HOLD";
        if (action !== actionFilter) return false;
      }
      if (timeframeFilter !== "ALL") {
        if (getSignalTimeframe(s.signal_type, s.strength) !== timeframeFilter) return false;
      }
      return true;
    });
  }, [signals, actionFilter, timeframeFilter]);

  useEffect(() => {
    api.health().then((h) => setMarketOpen(h.market_open)).catch(() => {});
    api.getPerformanceSummary()
      .then((res) => {
        if (res.data && res.data.total_evaluated > 0) {
          setPerfSummary(res.data);
        }
      })
      .catch(() => {});
    api.getMarketContext().then(setMarketCtx).catch(() => {});
    api.getIndices().then(setIndices).catch(() => {});
  }, []);

  const triggerScan = async () => {
    setScanning(true);
    try {
      await api.triggerScan();
      setCleared(false);
      await reload();
    } finally {
      setScanning(false);
    }
  };

  const handleMarkAllRead = () => {
    markAllRead();
    setCleared(true);
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full text-zinc-500 text-sm">
        Loading signals...
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-border bg-panel/50">
        <div className="flex items-center gap-2">
          <span className="text-xs text-zinc-400">
            {unreadCount > 0
              ? <span className="text-brand-light font-medium">{unreadCount} new</span>
              : "No new signals"}
          </span>
          {marketOpen !== null && (
            <span className={`text-[10px] px-1.5 py-0.5 rounded-full border font-medium
              ${marketOpen
                ? "border-profit/40 text-profit bg-profit/10"
                : "border-zinc-700 text-zinc-500"}`}>
              {marketOpen ? "Market Open" : "Market Closed"}
            </span>
          )}
        </div>
        <div className="flex gap-2">
          {unreadCount > 0 && (
            <button
              onClick={handleMarkAllRead}
              className="text-xs text-zinc-500 hover:text-zinc-300"
            >
              Mark all read
            </button>
          )}
          <button
            onClick={triggerScan}
            disabled={scanning}
            className="text-xs bg-brand/20 text-brand-light border border-brand/30 px-2 py-0.5 rounded hover:bg-brand/30 disabled:opacity-50"
          >
            {scanning ? "Scanning..." : "Scan Now"}
          </button>
        </div>
      </div>

      {/* Performance summary */}
      {perfSummary && (
        <div className="flex items-center justify-center gap-3 px-3 py-1.5 border-b border-border bg-zinc-900/40 text-[11px]">
          <span>
            Win Rate:{" "}
            <span className={perfSummary.win_rate >= 50 ? "text-profit font-medium" : "text-loss font-medium"}>
              {perfSummary.win_rate.toFixed(1)}%
            </span>
          </span>
          <span className="text-zinc-600">|</span>
          <span>
            Avg PnL:{" "}
            <span className={perfSummary.avg_pnl_pct >= 0 ? "text-profit font-medium" : "text-loss font-medium"}>
              {perfSummary.avg_pnl_pct >= 0 ? "+" : ""}{perfSummary.avg_pnl_pct.toFixed(1)}%
            </span>
          </span>
          <span className="text-zinc-600">|</span>
          <span className="text-zinc-400">
            Evaluated: {perfSummary.total_evaluated}
          </span>
        </div>
      )}

      {/* Market context bar: Indices + Regime + VIX + FII/DII */}
      {(indices || (marketCtx && (marketCtx.market_regime || marketCtx.india_vix != null || marketCtx.fii_dii))) && (
        <div className="flex items-center gap-2 px-3 py-1 border-b border-border bg-zinc-900/30 text-[10px] overflow-x-auto">
          {/* NIFTY 50 */}
          {indices?.["NIFTY 50"] && (
            <span className={`font-medium px-1.5 py-0.5 rounded border whitespace-nowrap ${
              (indices["NIFTY 50"].change_pct ?? 0) >= 0
                ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/25"
                : "bg-red-500/10 text-red-400 border-red-500/25"
            }`}>
              NIFTY {indices["NIFTY 50"].price?.toLocaleString("en-IN", { maximumFractionDigits: 0 })}{" "}
              <span className="opacity-80">
                {(indices["NIFTY 50"].change_pct ?? 0) >= 0 ? "+" : ""}{indices["NIFTY 50"].change_pct?.toFixed(1)}%
              </span>
            </span>
          )}
          {/* BSE SENSEX or NIFTY BANK (whichever is available) */}
          {(() => {
            const sensex = indices?.["BSE SENSEX"] || indices?.["NIFTY BANK"];
            const label = indices?.["BSE SENSEX"] ? "SENSEX" : "BANK NIFTY";
            if (!sensex) return null;
            return (
              <span className={`font-medium px-1.5 py-0.5 rounded border whitespace-nowrap ${
                (sensex.change_pct ?? 0) >= 0
                  ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/25"
                  : "bg-red-500/10 text-red-400 border-red-500/25"
              }`}>
                {label} {sensex.price?.toLocaleString("en-IN", { maximumFractionDigits: 0 })}{" "}
                <span className="opacity-80">
                  {(sensex.change_pct ?? 0) >= 0 ? "+" : ""}{sensex.change_pct?.toFixed(1)}%
                </span>
              </span>
            );
          })()}
          {marketCtx?.market_regime && (
            <span className={`font-bold px-1.5 py-0.5 rounded border ${REGIME_STYLES[marketCtx.market_regime.regime] || "bg-zinc-700/50 text-zinc-400 border-zinc-600"}`}>
              {marketCtx.market_regime.regime}
            </span>
          )}
          {marketCtx?.india_vix != null && (
            <span className={`font-medium px-1.5 py-0.5 rounded border ${
              marketCtx.india_vix > 20 ? "bg-orange-500/10 text-orange-400 border-orange-500/25"
                : marketCtx.india_vix < 14 ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/25"
                : "bg-zinc-700/40 text-zinc-400 border-zinc-600/30"
            }`}>
              VIX {marketCtx.india_vix.toFixed(1)}
            </span>
          )}
          {marketCtx?.fii_dii && marketCtx.fii_dii.fii_net != null && (
            <span className={`font-medium px-1.5 py-0.5 rounded border ${
              marketCtx.fii_dii.fii_net > 0
                ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/25"
                : "bg-red-500/10 text-red-400 border-red-500/25"
            }`}>
              FII {marketCtx.fii_dii.fii_net > 0 ? "+" : ""}{(marketCtx.fii_dii.fii_net / 100).toFixed(0)}Cr
            </span>
          )}
          {marketCtx?.fii_dii && marketCtx.fii_dii.dii_net != null && (
            <span className={`font-medium px-1.5 py-0.5 rounded border ${
              marketCtx.fii_dii.dii_net > 0
                ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/25"
                : "bg-red-500/10 text-red-400 border-red-500/25"
            }`}>
              DII {marketCtx.fii_dii.dii_net > 0 ? "+" : ""}{(marketCtx.fii_dii.dii_net / 100).toFixed(0)}Cr
            </span>
          )}
        </div>
      )}

      {/* Filters */}
      <div className="flex-shrink-0 flex items-center gap-1.5 px-3 py-1.5 border-b border-border overflow-x-auto">
        {(["ALL", "BUY", "SELL", "HOLD"] as ActionFilter[]).map((f) => (
          <button
            key={f}
            onClick={() => setActionFilter(f)}
            className={`text-[10px] font-semibold px-2 py-0.5 rounded-full border whitespace-nowrap transition-colors ${
              actionFilter === f
                ? f === "BUY" ? "bg-emerald-500/20 text-emerald-400 border-emerald-500/40"
                : f === "SELL" ? "bg-red-500/20 text-red-400 border-red-500/40"
                : f === "HOLD" ? "bg-amber-500/20 text-amber-400 border-amber-500/40"
                : "bg-brand/20 text-brand-light border-brand/40"
                : "text-zinc-500 border-zinc-700 hover:text-zinc-300"
            }`}
          >
            {f}
          </button>
        ))}
        <span className="text-zinc-700 mx-0.5">|</span>
        {(["ALL", "Intraday", "Swing", "Long-term"] as TimeframeFilter[]).map((f) => (
          <button
            key={f}
            onClick={() => setTimeframeFilter(f)}
            className={`text-[10px] font-semibold px-2 py-0.5 rounded-full border whitespace-nowrap transition-colors ${
              timeframeFilter === f
                ? f === "Intraday" ? "bg-blue-500/20 text-blue-400 border-blue-500/40"
                : f === "Swing" ? "bg-purple-500/20 text-purple-400 border-purple-500/40"
                : f === "Long-term" ? "bg-amber-500/20 text-amber-400 border-amber-500/40"
                : "bg-brand/20 text-brand-light border-brand/40"
                : "text-zinc-500 border-zinc-700 hover:text-zinc-300"
            }`}
          >
            {f}
          </button>
        ))}
      </div>

      {/* Signal list */}
      <div className="flex-1 overflow-y-auto px-3 py-2 flex flex-col">
        {error && (
          <div className="text-xs text-loss bg-loss/10 border border-loss/30 rounded p-2 mb-2">
            Backend error: {error}. Is the backend running?
          </div>
        )}

        {cleared ? (
          /* Cleared state — prompt user to scan for fresh insights */
          <div className="flex flex-col items-center justify-center h-full gap-4 text-zinc-500">
            <span className="text-4xl">✓</span>
            <div className="text-center">
              <p className="text-sm font-medium text-zinc-300">All caught up!</p>
              <p className="text-xs mt-1.5 text-zinc-500 max-w-[260px] leading-relaxed">
                All signals have been marked as read. Run a fresh scan to discover new trading opportunities.
              </p>
            </div>
            <button
              onClick={triggerScan}
              disabled={scanning}
              className="bg-brand text-white text-sm font-medium px-5 py-2 rounded-lg hover:bg-brand/80 disabled:opacity-50 transition-colors"
            >
              {scanning ? "Scanning..." : "Scan Now"}
            </button>
          </div>
        ) : filteredSignals.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full gap-3 text-zinc-500">
            <span className="text-4xl">📊</span>
            <div className="text-center">
              <p className="text-sm font-medium text-zinc-400">
                {signals.length === 0 ? "No signals yet" : "No signals match filters"}
              </p>
              <p className="text-xs mt-1">
                {signals.length === 0
                  ? "Click \"Scan Now\" or wait for the scheduled scan"
                  : `${signals.length} signals hidden by filters`}
              </p>
            </div>
          </div>
        ) : (
          filteredSignals.map((signal) => (
            <SignalCard
              key={signal.id}
              signal={signal}
              onRead={markRead}
              onDismiss={dismiss}
            />
          ))
        )}
        <div className="mt-auto pt-2 px-1">
          <Disclaimer />
        </div>
      </div>
    </div>
  );
}
