import { useEffect, useState, useMemo } from "react";
import SignalCard from "../components/SignalCard";
import { Disclaimer } from "../components/Disclaimer";
import TodaysPlan from "../components/TodaysPlan";
import NewsPanel from "../components/NewsPanel";
import InsightsCard from "../components/InsightsCard";
import LiveMacroSnapshot from "../components/LiveMacroSnapshot";
import { useSignals } from "../hooks/useSignals";
import { useAudioAlerts } from "../hooks/useAudioAlerts";
import { api } from "../../shared/api";
import { DIRECTION_ACTION, getSignalTimeframe } from "../../shared/constants";
import { getSettings } from "../../shared/storage";
import { deepLink } from "../../shared/localStore";
import { loadEdge } from "../../shared/edgeCache";
import type { AppSettings, ScanStatus } from "../../shared/types";

interface DashboardProps {
  onSelectSymbol?: (symbol: string) => void;
}

type ActionFilter = "ALL" | "BUY" | "SELL" | "HOLD";

function formatRelative(iso: string): string {
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return "";
  const diffMs = Date.now() - then;
  const mins = Math.floor(diffMs / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

type TimeframeFilter = "ALL" | "Intraday" | "Swing" | "Long-term";

interface PerformanceSummary {
  total_evaluated: number;
  total_wins: number;
  win_rate: number;
  avg_pnl_pct: number;
  window_days: number | null;
  last_evaluated_at: string | null;
}

interface MarketContext {
  fii_dii: { fii_net: number | null; dii_net: number | null; sentiment: string } | null;
  india_vix: number | null;
  market_regime: { regime: string; confidence: number; description: string } | null;
}

/**
 * Signal types that historically underperform in each regime — used by the
 * advisor-mode default filter to hide low-edge setups. Source: standard
 * trend/range follower playbook (don't fade strong trends, don't chase
 * breakouts in chop).
 */
const REGIME_SUPPRESS: Record<string, Set<string>> = {
  "Ranging": new Set([
    "breakout", "consolidation_breakout", "52_week_high", "52_week_low",
    "gap_up", "gap_down", "ema_crossover",
  ]),
  "Strong Bull": new Set([
    "rsi_extreme", "double_top", "head_and_shoulders", "shooting_star", "evening_star", "bearish_engulfing",
  ]),
  "Strong Bear": new Set([
    "rsi_extreme", "double_bottom", "inverse_head_and_shoulders", "hammer", "morning_star", "bullish_engulfing",
  ]),
  "Volatile": new Set([
    "narrow_range", "inside_day", "volume_dry_up",
  ]),
};

const REGIME_STYLES: Record<string, string> = {
  "Strong Bull": "bg-emerald-500/15 text-emerald-400 border-emerald-500/30",
  "Weak Bull": "bg-emerald-500/10 text-emerald-300 border-emerald-500/20",
  "Strong Bear": "bg-red-500/15 text-red-400 border-red-500/30",
  "Weak Bear": "bg-red-500/10 text-red-300 border-red-500/20",
  "Ranging": "bg-yellow-500/15 text-yellow-400 border-yellow-500/30",
  "Volatile": "bg-orange-500/15 text-orange-400 border-orange-500/30",
};

export default function Dashboard({ onSelectSymbol }: DashboardProps = {}) {
  const { signals, loading, error, unreadCount, markRead, markAllRead, dismiss, reload } = useSignals();
  useAudioAlerts(signals);
  const [mutedSymbols, setMutedSymbols] = useState<Set<string>>(new Set());
  const [mutedTypes, setMutedTypes] = useState<Set<string>>(new Set());
  const [snoozedUntil, setSnoozedUntil] = useState<number>(0);
  const [pinnedSignalId, setPinnedSignalId] = useState<string | null>(null);
  const [regimeFilterOn, setRegimeFilterOn] = useState<boolean>(true);
  const [regimeOverride, setRegimeOverride] = useState<boolean>(false); // session-only "show all"
  const [dedupeOn, setDedupeOn] = useState<boolean>(true);
  const [edgeBest, setEdgeBest] = useState<{ signal_type: string; avg_pnl: number; win_rate: number } | null>(null);
  const [edgeWorst, setEdgeWorst] = useState<{ signal_type: string; avg_pnl: number; win_rate: number } | null>(null);

  // Load mute/snooze settings
  useEffect(() => {
    (async () => {
      const s = (await getSettings()) as Partial<AppSettings>;
      setMutedSymbols(new Set(s.muted_symbols ?? []));
      setMutedTypes(new Set(s.muted_signal_types ?? []));
      setSnoozedUntil(s.snoozed_until ? Date.parse(s.snoozed_until) : 0);
      setRegimeFilterOn(s.regime_filter !== false);
      setDedupeOn(s.dedupe_signals !== false);
      const pinned = await deepLink.consumePinnedSignal();
      if (pinned) setPinnedSignalId(pinned);
    })();
  }, []);
  const [scanning, setScanning] = useState(false);
  const [scanProgress, setScanProgress] = useState<ScanStatus | null>(null);
  const [marketOpen, setMarketOpen] = useState<boolean | null>(null);
  const [perfSummary, setPerfSummary] = useState<PerformanceSummary | null>(null);
  const [marketCtx, setMarketCtx] = useState<MarketContext | null>(null);
  const [indices, setIndices] = useState<Record<string, { price: number; change: number; change_pct: number }> | null>(null);
  const [actionFilter, setActionFilter] = useState<ActionFilter>("ALL");
  const [timeframeFilter, setTimeframeFilter] = useState<TimeframeFilter>("ALL");

  // Action and timeframe are independent filter axes. We deliberately do NOT
  // auto-snap timeframe when the action changes: the old coupling (BUY→Long-term,
  // SELL→Swing) created unreachable filter combinations that hid valid signals
  // (e.g. the 214 active bearish-swing signals, and long-term BUYs). Users pick
  // each axis themselves; "ALL" on either axis means no constraint on that axis.
  const handleActionFilter = (f: ActionFilter) => {
    setActionFilter(f);
  };
  const handleTimeframeFilter = (f: TimeframeFilter) => {
    setTimeframeFilter(f);
  };
  const [cleared, setCleared] = useState(false);

  const regime = marketCtx?.market_regime?.regime;
  const suppressedTypes = regime && regimeFilterOn && !regimeOverride
    ? REGIME_SUPPRESS[regime] ?? null
    : null;

  // Deduplicate (symbol, day, direction) groups: keep the highest-strength
  // signal, attach merged_count + merged_types for the card to badge.
  const dedupedSignals = useMemo(() => {
    if (!dedupeOn) return signals;
    const groups = new Map<string, typeof signals>();
    for (const s of signals) {
      const day = s.created_at.slice(0, 10); // ISO YYYY-MM-DD
      const key = `${s.symbol.toUpperCase()}|${day}|${s.direction}`;
      const arr = groups.get(key) ?? [];
      arr.push(s);
      groups.set(key, arr);
    }
    const out: typeof signals = [];
    for (const arr of groups.values()) {
      if (arr.length === 1) {
        out.push(arr[0]);
        continue;
      }
      // Pick strongest; if tied, prefer confluence > others
      arr.sort((a, b) => {
        if (b.strength !== a.strength) return b.strength - a.strength;
        if (a.signal_type === "confluence") return -1;
        if (b.signal_type === "confluence") return 1;
        return 0;
      });
      const winner = arr[0];
      const rest = arr.slice(1);
      // Inherit unread state if any underlying signal is unread, so the user
      // sees fresh material (avoids "I already read this" feeling).
      const anyUnread = arr.some((s) => !s.read);
      out.push({
        ...winner,
        read: anyUnread ? false : winner.read,
        metadata: {
          ...(winner.metadata ?? {}),
          merged_count: arr.length,
          merged_types: rest.map((r) => r.signal_type),
          merged_ids: rest.map((r) => r.id),
        },
      });
    }
    // Preserve original ordering (newest first) by created_at desc
    out.sort((a, b) => (b.created_at < a.created_at ? -1 : 1));
    return out;
  }, [signals, dedupeOn]);

  const dedupedAway = signals.length - dedupedSignals.length;

  const filteredSignals = useMemo(() => {
    const snoozeActive = snoozedUntil > Date.now();
    return dedupedSignals.filter((s) => {
      if (snoozeActive && s.id !== pinnedSignalId) return false;
      if (mutedSymbols.has(s.symbol.toUpperCase())) return false;
      if (mutedTypes.has(s.signal_type)) return false;
      if (suppressedTypes?.has(s.signal_type)) return false;
      if (actionFilter !== "ALL") {
        const action = DIRECTION_ACTION[s.direction] || "HOLD";
        if (action !== actionFilter) return false;
      }
      if (timeframeFilter !== "ALL") {
        if (getSignalTimeframe(s.signal_type, s.strength) !== timeframeFilter) return false;
      }
      return true;
    });
  }, [dedupedSignals, actionFilter, timeframeFilter, mutedSymbols, mutedTypes, snoozedUntil, pinnedSignalId, suppressedTypes]);

  const suppressedCount = suppressedTypes
    ? dedupedSignals.filter((s) => suppressedTypes.has(s.signal_type)).length
    : 0;

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
    loadEdge()
      .then((e) => {
        if (!e.rows.length) return;
        // Top/bottom by avg_pnl, ignore tiny samples
        const sized = e.rows.filter((r) => r.trades >= 100);
        if (!sized.length) return;
        const sorted = [...sized].sort((a, b) => b.avg_pnl - a.avg_pnl);
        setEdgeBest(sorted[0]);
        setEdgeWorst(sorted[sorted.length - 1]);
      })
      .catch(() => {});
  }, []);

  const triggerScan = async () => {
    setScanning(true);
    setScanProgress(null);
    try {
      // Async pattern: trigger returns 202 + job_id in <1s; we poll the
      // status endpoint until the scan is done. Cap polling at 6 minutes
      // (real scans land at 160-200s; LLM judge adds 5-15s; safety margin
      // for a slow yfinance day).
      await api.triggerScan();
      const deadline = Date.now() + 6 * 60_000;
      // Tolerate a few consecutive status-poll failures — the scan worker
      // can briefly saturate the event loop and produce transient timeouts;
      // aborting the whole scan on the first one is too harsh.
      let consecutiveStatusErrors = 0;
      const MAX_STATUS_ERRORS = 5;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 2000));
        try {
          const status = await api.getScanStatus();
          consecutiveStatusErrors = 0;
          setScanProgress(status);
          if (status.status === "completed") break;
          if (status.status === "failed") {
            console.warn("[agentX] Scan failed:", status.error);
            break;
          }
        } catch (pollErr) {
          consecutiveStatusErrors += 1;
          if (consecutiveStatusErrors >= MAX_STATUS_ERRORS) {
            throw pollErr;
          }
          console.warn(
            `[agentX] Scan status poll failed (${consecutiveStatusErrors}/${MAX_STATUS_ERRORS}):`,
            pollErr instanceof Error ? pollErr.message : pollErr,
          );
        }
      }
      setCleared(false);
      await reload();
    } catch (e) {
      console.warn("[agentX] Scan request failed:", e instanceof Error ? e.message : e);
    } finally {
      setScanning(false);
      setScanProgress(null);
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
            {scanning
              ? (scanProgress && scanProgress.total_symbols > 0
                  ? `Scanning ${scanProgress.completed_symbols}/${scanProgress.total_symbols}…`
                  : "Scanning…")
              : "Scan Now"}
          </button>
        </div>
      </div>

      {/* Performance summary — rolling window so numbers move as outcomes
          evaluate. Lifetime aggregates barely shift once n grows past a
          few thousand, which made the bar feel static. */}
      {perfSummary && (
        <div
          className="flex items-center justify-center gap-3 px-3 py-1.5 border-b border-border bg-zinc-900/40 text-[11px]"
          title={
            perfSummary.last_evaluated_at
              ? `Last evaluation: ${new Date(perfSummary.last_evaluated_at).toLocaleString()}`
              : undefined
          }
        >
          <span className="text-zinc-500">
            {perfSummary.window_days ? `Last ${perfSummary.window_days}d` : "All-time"}
          </span>
          <span className="text-zinc-600">·</span>
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
          {perfSummary.last_evaluated_at && (
            <>
              <span className="text-zinc-600">·</span>
              <span className="text-zinc-500">
                updated {formatRelative(perfSummary.last_evaluated_at)}
              </span>
            </>
          )}
        </div>
      )}

      {/* Backtest edge — best/worst signal types */}
      {(edgeBest || edgeWorst) && (
        <div className="flex items-center justify-center gap-3 px-3 py-1 border-b border-border bg-zinc-900/30 text-[10px]">
          {edgeBest && (
            <span title="Best 5d avg PnL in the latest internal backtest">
              <span className="text-zinc-500">Edge ★ </span>
              <span className="text-profit font-medium">{edgeBest.signal_type}</span>
              <span className="text-zinc-500"> ({edgeBest.win_rate.toFixed(0)}% WR · +{edgeBest.avg_pnl.toFixed(2)}%)</span>
            </span>
          )}
          {edgeBest && edgeWorst && <span className="text-zinc-700">|</span>}
          {edgeWorst && (
            <span title="Worst 5d avg PnL — treat with skepticism">
              <span className="text-zinc-500">Avoid </span>
              <span className="text-loss font-medium">{edgeWorst.signal_type}</span>
              <span className="text-zinc-500"> ({edgeWorst.win_rate.toFixed(0)}% WR · {edgeWorst.avg_pnl.toFixed(2)}%)</span>
            </span>
          )}
        </div>
      )}

      <LiveMacroSnapshot />

      <div className="responsive-top">
        {/* Agent insights — recommendations from the autonomous loop */}
        <InsightsCard />

        {/* Today's plan card */}
        <TodaysPlan
          signals={signals}
          marketRegime={marketCtx?.market_regime?.regime ?? null}
          marketOpen={marketOpen}
          onSelectSymbol={onSelectSymbol}
        />
      </div>

      {/* Market context bar: Indices + Regime + VIX (FII/DII live in Live Macro) */}
      {(indices || (marketCtx && (marketCtx.market_regime || marketCtx.india_vix != null))) && (
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
          {/* BSE SENSEX + NIFTY BANK — render whichever ones the backend returned */}
          {(["BSE SENSEX", "NIFTY BANK"] as const).map((key) => {
            const idx = indices?.[key];
            if (!idx || idx.price == null) return null;
            const label = key === "BSE SENSEX" ? "SENSEX" : "BANK NIFTY";
            const up = (idx.change_pct ?? 0) >= 0;
            return (
              <span
                key={key}
                className={`font-medium px-1.5 py-0.5 rounded border whitespace-nowrap ${
                  up
                    ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/25"
                    : "bg-red-500/10 text-red-400 border-red-500/25"
                }`}
              >
                {label} {idx.price.toLocaleString("en-IN", { maximumFractionDigits: 0 })}{" "}
                <span className="opacity-80">
                  {up ? "+" : ""}{idx.change_pct?.toFixed(1)}%
                </span>
              </span>
            );
          })}
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
          {/* FII/DII pills intentionally omitted here — the Live Macro panel
              already shows FII net / DII net, so repeating them is redundant. */}
        </div>
      )}

      {/* News (collapsed by default) */}
      <NewsPanel collapsedDefault={true} />

      {/* Dedup banner — only when actually compressing */}
      {dedupeOn && dedupedAway > 0 && (
        <div className="flex items-center justify-between px-3 py-1 border-b border-border bg-zinc-800/40 text-[10px]">
          <span className="text-zinc-400">
            Deduped {dedupedAway} same-symbol/same-day {dedupedAway === 1 ? "signal" : "signals"}
          </span>
          <button
            onClick={() => setDedupeOn(false)}
            className="text-brand-light hover:text-zinc-100 underline decoration-dotted"
          >
            Show all
          </button>
        </div>
      )}

      {/* Regime suppression banner — only visible when active */}
      {suppressedTypes && suppressedCount > 0 && (
        <div className="flex items-center justify-between px-3 py-1 border-b border-border bg-amber-500/5 text-[10px]">
          <span className="text-amber-300/90">
            Regime · {regime}: hiding {suppressedCount} low-edge {suppressedCount === 1 ? "signal" : "signals"}
          </span>
          <button
            onClick={() => setRegimeOverride(true)}
            className="text-amber-200 hover:text-zinc-100 underline decoration-dotted"
          >
            Show all
          </button>
        </div>
      )}
      {regimeOverride && (
        <div className="flex items-center justify-between px-3 py-1 border-b border-border bg-zinc-800/40 text-[10px]">
          <span className="text-zinc-400">Regime filter overridden for this session</span>
          <button onClick={() => setRegimeOverride(false)} className="text-brand-light hover:text-zinc-100">
            Re-enable
          </button>
        </div>
      )}

      {/* Filters */}
      <div className="flex-shrink-0 flex items-center gap-1.5 px-3 py-1.5 border-b border-border overflow-x-auto">
        {(["ALL", "BUY", "SELL", "HOLD"] as ActionFilter[]).map((f) => (
          <button
            key={f}
            onClick={() => handleActionFilter(f)}
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
            onClick={() => handleTimeframeFilter(f)}
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
      <div className="flex-1 overflow-y-auto px-3 py-2 flex flex-col responsive-content-cap">
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
              {scanning
              ? (scanProgress && scanProgress.total_symbols > 0
                  ? `Scanning ${scanProgress.completed_symbols}/${scanProgress.total_symbols}…`
                  : "Scanning…")
              : "Scan Now"}
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
          <div className="responsive-grid">
            {filteredSignals.map((signal) => (
              <SignalCard
                key={signal.id}
                signal={signal}
                onRead={markRead}
                onDismiss={dismiss}
              />
            ))}
          </div>
        )}
        <div className="mt-auto pt-2 px-1">
          <Disclaimer />
        </div>
      </div>
    </div>
  );
}
