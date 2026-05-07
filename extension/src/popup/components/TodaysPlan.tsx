import { useEffect, useMemo, useState } from "react";
import { api } from "../../shared/api";
import type { Signal, WatchlistItem, CorporateAction } from "../../shared/types";
import { DIRECTION_ACTION } from "../../shared/constants";

interface Props {
  signals: Signal[];
  marketRegime?: string | null;
  marketOpen: boolean | null;
  onSelectSymbol?: (symbol: string) => void;
}

/**
 * Compact 'Today's plan' card. Aggregates:
 *  - high-conviction signals on watchlist symbols
 *  - upcoming earnings/corporate-actions in next 7 days for watchlist
 *  - market regime
 */
export default function TodaysPlan({ signals, marketRegime, marketOpen, onSelectSymbol }: Props) {
  const [watchlist, setWatchlist] = useState<WatchlistItem[] | null>(null);
  const [actions, setActions] = useState<CorporateAction[] | null>(null);

  useEffect(() => {
    api.getWatchlist().then((r) => setWatchlist(r.watchlist)).catch(() => setWatchlist([]));
    api.getCorporateActions().then((r) => setActions(r.actions)).catch(() => setActions([]));
  }, []);

  const watchSet = useMemo(
    () => new Set((watchlist ?? []).map((w) => w.symbol.toUpperCase())),
    [watchlist]
  );

  const watchSignals = useMemo(
    () =>
      signals
        .filter((s) => watchSet.has(s.symbol.toUpperCase()) && s.strength >= 6 && !s.dismissed)
        .slice(0, 3),
    [signals, watchSet]
  );

  const upcomingActions = useMemo(() => {
    if (!actions) return [];
    const now = Date.now();
    const horizon = now + 7 * 24 * 60 * 60 * 1000;
    return actions
      .filter((a) => watchSet.has(a.symbol?.toUpperCase()))
      .filter((a) => {
        const ts = a.ex_date ? Date.parse(a.ex_date) : 0;
        return ts && ts >= now && ts <= horizon;
      })
      .slice(0, 3);
  }, [actions, watchSet]);

  // Hide entirely when there's nothing to say
  if (
    !watchSignals.length &&
    !upcomingActions.length &&
    !marketRegime &&
    marketOpen == null
  ) {
    return null;
  }

  return (
    <div className="px-3 py-2 border-b border-border bg-zinc-900/40">
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-[10px] font-semibold text-zinc-400 uppercase tracking-wider">Today's plan</span>
        <span className="text-[10px] text-zinc-600">{new Date().toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" })}</span>
      </div>
      <div className="space-y-1 text-[11px] text-zinc-300 leading-relaxed">
        {marketOpen != null && (
          <div>
            <span className="text-zinc-500">Market:</span>{" "}
            <span className={marketOpen ? "text-profit" : "text-zinc-400"}>
              {marketOpen ? "Open" : "Closed"}
            </span>
            {marketRegime && (
              <>
                {" · "}
                <span className="text-zinc-500">Regime:</span>{" "}
                <span className="text-brand-light font-medium">{marketRegime}</span>
              </>
            )}
          </div>
        )}
        {watchSignals.length > 0 && (
          <div>
            <span className="text-zinc-500">{watchSignals.length} watchlist setup{watchSignals.length > 1 ? "s" : ""}:</span>{" "}
            {watchSignals.map((s, i) => {
              const action = DIRECTION_ACTION[s.direction] || "WATCH";
              const color = action === "BUY" ? "text-profit" : action === "SELL" ? "text-loss" : "text-amber-400";
              return (
                <span key={s.id}>
                  <button
                    onClick={() => onSelectSymbol?.(s.symbol)}
                    className="font-semibold text-zinc-100 hover:text-brand-light underline decoration-dotted underline-offset-2"
                  >
                    {s.symbol}
                  </button>
                  <span className={`${color} text-[10px] ml-0.5`}>·{action}</span>
                  {i < watchSignals.length - 1 ? ", " : ""}
                </span>
              );
            })}
          </div>
        )}
        {upcomingActions.length > 0 && (
          <div>
            <span className="text-zinc-500">Coming up:</span>{" "}
            {upcomingActions.map((a, i) => (
              <span key={`${a.symbol}-${a.action_type}-${i}`}>
                <span className="font-semibold text-zinc-100">{a.symbol}</span>{" "}
                <span className="text-zinc-400">{a.action_type}</span>
                {a.ex_date && <span className="text-zinc-600"> · {new Date(a.ex_date).toLocaleDateString("en-IN", { day: "numeric", month: "short" })}</span>}
                {i < upcomingActions.length - 1 ? ", " : ""}
              </span>
            ))}
          </div>
        )}
        {!watchSignals.length && !upcomingActions.length && (
          <div className="text-zinc-500">
            No high-conviction watchlist setups today. Try a fresh scan or browse the screener.
          </div>
        )}
      </div>
    </div>
  );
}
