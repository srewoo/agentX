import { useEffect, useState } from "react";
import type { Signal, AppSettings, PaperTrade, SignalEdgeRow, DeepSignalAnalysis } from "../../shared/types";
import { SIGNAL_TYPE_LABELS, DIRECTION_ACTION, ACTION_COLORS, getSignalTimeframe } from "../../shared/constants";
import { api } from "../../shared/api";
import { useExchange } from "../lib/ExchangeContext";
import { getSettings, saveSettings } from "../../shared/storage";
import { paperTrades } from "../../shared/localStore";
import { getEdgeFor } from "../../shared/edgeCache";
import MiniChart from "./MiniChart";
import SignalChatDrawer from "./SignalChatDrawer";

interface Props {
  signal: Signal;
  onRead: (id: string) => void;
  onDismiss: (id: string) => void;
}

interface RiskPlan {
  target?: number;
  stop?: number;
  qty?: number;
  riskPerShare?: number;
  riskAmount?: number;
  rewardAmount?: number;
  rr?: number;
  source: "atr" | "heuristic";
}

/**
 * Compute target / SL using ATR (volatility-adjusted) when available,
 * falling back to a fixed % heuristic otherwise. Position size follows
 * the Van Tharp risk-per-trade rule: qty = (capital × risk%) / (entry − stop).
 */
function computeRiskPlan(
  signal: Signal,
  atr: number | null,
  settings: Partial<AppSettings>
): RiskPlan {
  const price = signal.current_price;
  if (price == null || price <= 0) return { source: "heuristic" };

  const slMult = settings.atr_sl_mult ?? 1.5;
  const tgtMult = settings.atr_target_mult ?? 3.0;
  const capital = settings.capital ?? 100000;
  const riskPct = (settings.risk_per_trade_pct ?? 1.0) / 100;

  // ATR-based when available; else %-based heuristic preserving the same R:R
  const useAtr = atr != null && atr > 0;
  const riskPerShare = useAtr ? atr * slMult : price * 0.02 * slMult;
  const rewardPerShare = useAtr ? atr * tgtMult : price * 0.02 * tgtMult;

  const dir = signal.direction === "bearish" ? -1 : signal.direction === "bullish" ? 1 : 0;
  if (dir === 0) {
    // Neutral signals: show levels but don't suggest a trade.
    return { source: useAtr ? "atr" : "heuristic" };
  }
  const stop = price - dir * riskPerShare;
  const target = price + dir * rewardPerShare;

  const riskAmount = capital * riskPct;
  const qty = Math.max(0, Math.floor(riskAmount / riskPerShare));
  const rewardAmount = qty * rewardPerShare;
  const rr = riskPerShare > 0 ? rewardPerShare / riskPerShare : undefined;

  return {
    target,
    stop,
    qty,
    riskPerShare,
    riskAmount: qty * riskPerShare, // actual ₹ risk at suggested qty
    rewardAmount,
    rr,
    source: useAtr ? "atr" : "heuristic",
  };
}

function timeAgo(isoDate: string): string {
  const diff = Date.now() - new Date(isoDate).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

/**
 * A signal is "stale" if it was generated before today's NSE market open
 * (9:15 IST) and is still unread. The price level it called is no longer
 * tradeable at the original entry, so we flag it loudly. Read signals are
 * never marked stale — the user has already acknowledged them.
 */
function isStale(isoDate: string, read: boolean): boolean {
  if (read) return false;
  const created = new Date(isoDate).getTime();
  // 9:15 IST today, expressed in UTC ms
  const IST_OFFSET_MS = (5 * 60 + 30) * 60 * 1000;
  const istNow = new Date(Date.now() + IST_OFFSET_MS);
  const ymd = Date.UTC(istNow.getUTCFullYear(), istNow.getUTCMonth(), istNow.getUTCDate());
  const todayOpenUTC = ymd + (9 * 60 + 15) * 60 * 1000 - IST_OFFSET_MS;
  // If we're still before today's open, fall back to "older than 18h" so signals
  // from yesterday's session don't show as fresh at 8 AM.
  const cutoff = Date.now() < todayOpenUTC ? Date.now() - 18 * 60 * 60 * 1000 : todayOpenUTC;
  return created < cutoff;
}

// Use shared getSignalTimeframe from constants to avoid duplication

const TIMEFRAME_STYLE: Record<string, string> = {
  Intraday: "bg-blue-500/15 text-blue-400 border-blue-500/30",
  Swing: "bg-purple-500/15 text-purple-400 border-purple-500/30",
  "Long-term": "bg-amber-500/15 text-amber-400 border-amber-500/30",
};

export default function SignalCard({ signal, onRead, onDismiss }: Props) {
  const exchange = useExchange();
  const [expanded, setExpanded] = useState(false);
  const [chatOpen, setChatOpen] = useState(false);
  const [tradeAdded, setTradeAdded] = useState(false);
  const [muteMsg, setMuteMsg] = useState<string | null>(null);
  const [atr, setAtr] = useState<number | null>(
    typeof signal.metadata?.atr === "number" ? (signal.metadata.atr as number) : null
  );
  const [settings, setSettings] = useState<Partial<AppSettings>>({});
  const [edge, setEdge] = useState<SignalEdgeRow | null>(null);
  const [deepAnalysis, setDeepAnalysis] = useState<DeepSignalAnalysis | null>(null);
  const [thinking, setThinking] = useState(false);
  const [thinkingError, setThinkingError] = useState<string | null>(null);

  // Load advisor settings + lazy-fetch ATR on first expand if not in metadata
  useEffect(() => {
    getSettings().then(setSettings);
    if (signal.direction !== "neutral") {
      getEdgeFor(signal.signal_type, signal.direction).then(setEdge);
    }
  }, [signal.signal_type, signal.direction]);
  useEffect(() => {
    if (!expanded || atr != null) return;
    api.getTechnicals(signal.symbol, exchange)
      .then((t) => { if (typeof t.atr === "number") setAtr(t.atr); })
      .catch(() => { /* fallback to heuristic remains */ });
  }, [expanded, atr, signal.symbol, exchange]);

  // Layer-1 (rule-based) action — what the deterministic engine produced.
  const ruleAction = DIRECTION_ACTION[signal.direction] || "HOLD";
  // Layer-2 (LLM judge) overrides:
  //  - "drop": LLM disagreed, force HOLD so the user doesn't act on it.
  //  - "downgrade": LLM kept the direction but flagged it as low-conviction —
  //    keep the rule action but mute the colour so it doesn't read as a
  //    confident BUY/SELL.
  //  - "keep" / null: trust the rule engine, render normally.
  // Layer-3 (Bull/Bear/Judge debate) overrides — only present on the top-N
  // strong signals when debate_enabled. Treated as an *additional* veto:
  //  - rule says bullish but debate winner = "bear" → flip to HOLD
  //  - rule says bearish but debate winner = "bull" → flip to HOLD
  //  - winner == "inconclusive"        → demote like downgrade (muted, ↓)
  //  - winner agrees with rule direction → no override (trust both layers)
  const debateContradicts =
    (signal.direction === "bullish" && signal.debate_winner === "bear") ||
    (signal.direction === "bearish" && signal.debate_winner === "bull");
  const debateInconclusive = signal.debate_winner === "inconclusive";

  const droppedByJudge = signal.llm_verdict === "drop";
  const downgradedByJudge = signal.llm_verdict === "downgrade";

  const action =
    droppedByJudge || debateContradicts ? "HOLD" : ruleAction;
  const baseActionColor = ACTION_COLORS[action] || "#F59E0B";
  const actionColor =
    downgradedByJudge || debateInconclusive ? "#9CA3AF" : baseActionColor;
  const actionDemoted =
    droppedByJudge || downgradedByJudge || debateContradicts || debateInconclusive;
  const timeframe = getSignalTimeframe(signal.signal_type, signal.strength);
  const label = SIGNAL_TYPE_LABELS[signal.signal_type] || signal.signal_type;
  const plan = computeRiskPlan(signal, atr, settings);
  const { target, stop, qty, riskAmount, rewardAmount, rr, source } = plan;

  const muteSymbol = async () => {
    const s = (await getSettings()) as Partial<AppSettings>;
    const list = new Set([...(s.muted_symbols ?? []), signal.symbol.toUpperCase()]);
    await saveSettings({ ...s, muted_symbols: Array.from(list) });
    setMuteMsg(`Muted ${signal.symbol}. Reload signals to apply.`);
  };

  const muteType = async () => {
    const s = (await getSettings()) as Partial<AppSettings>;
    const list = new Set([...(s.muted_signal_types ?? []), signal.signal_type]);
    await saveSettings({ ...s, muted_signal_types: Array.from(list) });
    setMuteMsg(`Muted "${label}". Reload signals to apply.`);
  };

  const takeTrade = async () => {
    if (!signal.current_price) return;
    const t: PaperTrade = {
      id: crypto.randomUUID(),
      symbol: signal.symbol,
      side: signal.direction === "bearish" ? "SELL" : "BUY",
      qty: qty && qty > 0 ? qty : 1,
      entry_price: signal.current_price,
      entry_at: new Date().toISOString(),
      signal_id: signal.id,
      target,
      stop_loss: stop,
      status: "open",
    };
    await paperTrades.add(t);
    setTradeAdded(true);
    setTimeout(() => setTradeAdded(false), 2500);
  };

  const runThinkingAnalysis = async () => {
    setThinking(true);
    setThinkingError(null);
    try {
      const response = await api.deepSignalAnalysis(signal.id, "medium");
      setDeepAnalysis(response.data);
    } catch (err) {
      setThinkingError(err instanceof Error ? err.message : "Thinking analysis failed");
    } finally {
      setThinking(false);
    }
  };

  const handleExpand = () => {
    setExpanded((e) => !e);
    if (!signal.read) {
      api.markRead(signal.id).catch(() => {});
      onRead(signal.id);
    }
  };

  return (
    <div
      role="button"
      tabIndex={0}
      aria-expanded={expanded}
      aria-label={`${signal.symbol} ${action} signal, strength ${signal.strength} out of 10. ${label}. ${signal.read ? "Read" : "Unread"}`}
      className={`rounded-xl border cursor-pointer px-3.5 py-3 mb-2.5
        ${signal.read ? "border-border bg-panel" : "border-brand/40 bg-zinc-800/80"}`}
      onClick={handleExpand}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); handleExpand(); } }}
    >
      {/* Row 1: Symbol + BUY/SELL/HOLD badge + time. flex-wrap so the LLM
          verdict chip doesn't crash into the right-hand timestamp/strength
          bars on narrow cards (3-up grid in standalone popup). */}
      <div className="flex items-center justify-between gap-2 mb-2 flex-wrap">
        <div className="flex items-center gap-2 flex-wrap min-w-0">
          {!signal.read && (
            <span className="w-2 h-2 rounded-full bg-brand-light flex-shrink-0" aria-hidden="true" />
          )}
          <span className="font-bold text-base text-zinc-100">{signal.symbol}</span>

          {/* BUY / SELL / HOLD badge — respects llm_verdict + debate overrides */}
          <span
            className="text-xs font-bold px-2 py-0.5 rounded-md border whitespace-nowrap"
            style={{
              color: actionColor,
              borderColor: `${actionColor}40`,
              backgroundColor: `${actionColor}15`,
            }}
            title={(() => {
              if (droppedByJudge) {
                return `LLM judge overrode rule (${ruleAction}) → HOLD. ${signal.llm_reason || "Low-conviction context."}`;
              }
              if (debateContradicts) {
                return `Bull/Bear debate winner = ${signal.debate_winner} contradicts rule (${ruleAction}) → HOLD. ${signal.debate_synthesis || ""}`;
              }
              if (downgradedByJudge) {
                return `LLM judge kept ${ruleAction} but flagged low-conviction. ${signal.llm_reason || ""}`;
              }
              if (debateInconclusive) {
                return `Bull/Bear debate was inconclusive — treat ${ruleAction} as low-conviction. ${signal.debate_synthesis || ""}`;
              }
              return undefined;
            })()}
          >
            {action}
            {actionDemoted && <span className="ml-1 opacity-70">↓</span>}
          </span>

          {/* Timeframe tag */}
          <span className={`text-[11px] font-medium px-1.5 py-0.5 rounded border ${TIMEFRAME_STYLE[timeframe]}`}>
            {timeframe}
          </span>

          {/* Layer-2 LLM judge verdict — compact chip so it fits next to the
              symbol/action/timeframe on a 3-up grid. Colour conveys verdict;
              the leading "AI" tag + glyph keeps it scannable without the
              verbose "LLM: DOWNGRADE" string that previously wrapped. */}
          {signal.llm_verdict && (
            <span
              className={`text-[10px] font-semibold px-1.5 py-0.5 rounded border whitespace-nowrap leading-none inline-flex items-center gap-1 ${
                signal.llm_verdict === "drop"
                  ? "bg-red-500/15 text-red-400 border-red-500/30"
                  : signal.llm_verdict === "downgrade"
                    ? "bg-amber-500/15 text-amber-400 border-amber-500/30"
                    : "bg-emerald-500/10 text-emerald-400 border-emerald-500/30"
              }`}
              title={
                signal.llm_reason ||
                (signal.llm_verdict === "drop"
                  ? "LLM judge: drop this signal"
                  : signal.llm_verdict === "downgrade"
                    ? "LLM judge: low conviction"
                    : "LLM judge endorsed this signal")
              }
            >
              <span className="opacity-60">AI</span>
              <span>
                {signal.llm_verdict === "drop" ? "✕" : signal.llm_verdict === "downgrade" ? "↓" : "✓"}
              </span>
            </span>
          )}

          {/* Layer-3 debate verdict chip — green when the debate winner
              matches the rule direction, red when it contradicts, amber
              when inconclusive. Only renders when debate ran on this
              signal (top-N high-conviction cohort). */}
          {signal.debate_winner && (
            <span
              className={`text-[10px] font-semibold px-1.5 py-0.5 rounded border whitespace-nowrap leading-none inline-flex items-center gap-1 ${
                debateContradicts
                  ? "bg-red-500/15 text-red-400 border-red-500/30"
                  : debateInconclusive
                    ? "bg-amber-500/15 text-amber-400 border-amber-500/30"
                    : "bg-emerald-500/10 text-emerald-400 border-emerald-500/30"
              }`}
              title={
                signal.debate_synthesis ||
                (debateContradicts
                  ? `Debate: ${signal.debate_winner} (contradicts rule)`
                  : debateInconclusive
                    ? "Debate: inconclusive"
                    : `Debate: ${signal.debate_winner} (confirms rule)`)
              }
            >
              <span className="opacity-60">DBT</span>
              <span>
                {debateContradicts ? "✕" : debateInconclusive ? "↓" : "✓"}
              </span>
            </span>
          )}
        </div>

        <div className="flex items-center gap-2 flex-shrink-0">
          {isStale(signal.created_at, signal.read) && (
            <span
              className="text-[10px] font-semibold px-1.5 py-0.5 rounded border bg-amber-500/10 text-amber-400 border-amber-500/30"
              title="This signal fired before today's market open. The entry level may no longer be valid — re-check before acting."
            >
              STALE
            </span>
          )}
          <span className="text-xs text-zinc-500 whitespace-nowrap">{timeAgo(signal.created_at)}</span>
          {/* Strength bar */}
          <div className="flex gap-0.5">
            {Array.from({ length: 5 }).map((_, i) => (
              <div
                key={i}
                className="w-1.5 h-3 rounded-sm"
                style={{
                  backgroundColor:
                    i < Math.ceil(signal.strength / 2) ? actionColor : "#3F3F46",
                }}
              />
            ))}
          </div>
        </div>
      </div>

      {/* Row 2: Signal type + metadata badges + price */}
      <div className="flex items-center justify-between mb-1.5">
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="text-xs text-zinc-500 bg-zinc-800 px-2 py-0.5 rounded-md">
            {label}
          </span>
          {/* Confluence badge */}
          {signal.signal_type === "confluence" && (
            <span className="text-[10px] font-bold px-1.5 py-0.5 rounded border bg-violet-500/15 text-violet-400 border-violet-500/30">
              {(signal.metadata?.signal_count as number) || 2}x CONFLUENCE
            </span>
          )}
          {/* Merged-count badge (signal dedup) */}
          {Boolean(signal.metadata?.merged_count) && (signal.metadata!.merged_count as number) > 1 && (
            <span
              className="text-[10px] font-semibold px-1.5 py-0.5 rounded border bg-zinc-700/40 text-zinc-300 border-zinc-600/40"
              title={`Also fired today: ${(signal.metadata!.merged_types as string[] | undefined)?.join(", ") || ""}`}
            >
              +{(signal.metadata!.merged_count as number) - 1} more
            </span>
          )}
          {/* Historical edge chip (from internal backtest) */}
          {edge && (
            <span
              className={`text-[10px] font-semibold px-1.5 py-0.5 rounded border ${
                edge.avg_pnl > 0.5 ? "bg-emerald-500/15 text-emerald-400 border-emerald-500/30"
                  : edge.avg_pnl >= 0 ? "bg-zinc-700/50 text-zinc-300 border-zinc-600/40"
                  : "bg-red-500/10 text-red-400 border-red-500/30"
              }`}
              title={`Backtest 5d: ${edge.win_rate.toFixed(1)}% WR, ${edge.avg_pnl >= 0 ? "+" : ""}${edge.avg_pnl.toFixed(2)}% avg PnL across ${edge.trades} historical trades`}
            >
              {edge.avg_pnl >= 0 ? "+" : ""}{edge.avg_pnl.toFixed(2)}% edge
            </span>
          )}
          {/* FII flow indicator */}
          {signal.metadata?.fii_modifier != null && (signal.metadata.fii_modifier as number) !== 0 && (
            <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded border ${
              (signal.metadata.fii_modifier as number) < 0
                ? "bg-red-500/15 text-red-400 border-red-500/30"
                : "bg-emerald-500/15 text-emerald-400 border-emerald-500/30"
            }`}>
              FII {(signal.metadata.fii_modifier as number) < 0 ? "SELLING" : "BUYING"}
            </span>
          )}
          {/* Delivery % badge for volume spikes */}
          {signal.signal_type === "volume_spike" && signal.metadata?.delivery_pct != null && (
            <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded border ${
              (signal.metadata.delivery_pct as number) > 60
                ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/25"
                : (signal.metadata.delivery_pct as number) < 30
                ? "bg-yellow-500/10 text-yellow-400 border-yellow-500/25"
                : "bg-zinc-700/50 text-zinc-400 border-zinc-600/30"
            }`}>
              {(signal.metadata.delivery_pct as number).toFixed(0)}% Delivery
            </span>
          )}
          {/* Counter-trend warning */}
          {Boolean(signal.metadata?.conflicting_signals) && (
            <span className="text-[10px] font-medium px-1.5 py-0.5 rounded border bg-yellow-500/10 text-yellow-400 border-yellow-500/25">
              MIXED
            </span>
          )}
        </div>
        {signal.current_price && (
          <span className="text-xs text-zinc-400 font-medium">
            ₹{signal.current_price.toLocaleString("en-IN")}
          </span>
        )}
      </div>

      {/* Row 3: Reason */}
      <p className="text-xs text-zinc-300 leading-relaxed line-clamp-2">
        {signal.reason}
      </p>

      {/* Expanded: LLM summary + risk */}
      {expanded && (
        <div className="mt-2.5 pt-2.5 border-t border-border space-y-2">
          {edge && (
            <div className={`text-[11px] rounded p-2 leading-relaxed ${
              edge.avg_pnl > 0.5 ? "bg-emerald-500/10 border border-emerald-500/20 text-zinc-300"
                : edge.avg_pnl >= 0 ? "bg-zinc-900/60 text-zinc-400"
                : "bg-red-500/10 border border-red-500/20 text-zinc-300"
            }`}>
              <span className="font-semibold">Historical edge: </span>
              {edge.win_rate.toFixed(1)}% win rate · {edge.avg_pnl >= 0 ? "+" : ""}{edge.avg_pnl.toFixed(2)}% avg PnL @ 5d ({edge.trades} backtested trades)
              {edge.avg_pnl < 0 && (
                <span className="block text-[10px] mt-0.5 italic text-loss/80">
                  This setup historically lost money. Treat with skepticism.
                </span>
              )}
            </div>
          )}
          {signal.llm_summary && (
            <div className="text-xs text-zinc-200 leading-relaxed bg-zinc-900/60 rounded-lg p-2.5">
              <span className="text-brand-light font-semibold">AI Insight: </span>
              {signal.llm_summary}
            </div>
          )}
          {signal.llm_verdict && signal.llm_reason && (
            <div
              className={`text-xs leading-relaxed rounded-lg p-2.5 border ${
                signal.llm_verdict === "drop"
                  ? "bg-red-500/10 border-red-500/20 text-zinc-200"
                  : signal.llm_verdict === "downgrade"
                    ? "bg-amber-500/10 border-amber-500/20 text-zinc-200"
                    : "bg-emerald-500/5 border-emerald-500/20 text-zinc-200"
              }`}
            >
              <span className="font-semibold">
                LLM review ({signal.llm_verdict}):
              </span>{" "}
              {signal.llm_reason}
            </div>
          )}
          {/* Multi-perspective analyst breakdown — only when multi_perspective
              ran for this signal. Shows synth + per-perspective contributions. */}
          {signal.mp_synthesis && (
            <div className="text-xs leading-relaxed rounded-lg p-2.5 border bg-violet-500/5 border-violet-500/20 text-zinc-200 space-y-1.5">
              <div className="flex items-center justify-between gap-2">
                <span className="font-semibold text-violet-300">Specialist desks</span>
                <span className="text-[10px] text-zinc-400">
                  {signal.mp_consensus?.replace(/_/g, " ")} · score{" "}
                  {(signal.mp_aggregate_score ?? 0).toFixed(2)}
                </span>
              </div>
              <p>{signal.mp_synthesis}</p>
              {signal.mp_perspectives_json && (
                <div className="grid grid-cols-2 gap-1.5 mt-1">
                  {(() => {
                    try {
                      const parsed = JSON.parse(signal.mp_perspectives_json) as Array<{
                        perspective: string; score: number; confidence: number; summary: string;
                      }>;
                      return parsed.map((p) => (
                        <div
                          key={p.perspective}
                          className={`rounded border p-1.5 ${
                            p.score > 0.15
                              ? "border-emerald-500/30 bg-emerald-500/5"
                              : p.score < -0.15
                                ? "border-red-500/30 bg-red-500/5"
                                : "border-zinc-700 bg-zinc-900/40"
                          }`}
                        >
                          <div className="flex items-center justify-between">
                            <span className="text-[10px] uppercase tracking-wide text-zinc-400">
                              {p.perspective}
                            </span>
                            <span
                              className={`text-[10px] font-bold ${
                                p.score >= 0 ? "text-emerald-400" : "text-red-400"
                              }`}
                            >
                              {p.score >= 0 ? "+" : ""}{p.score.toFixed(2)}
                            </span>
                          </div>
                          <p className="text-[11px] text-zinc-300 mt-0.5 leading-snug">
                            {p.summary}
                          </p>
                        </div>
                      ));
                    } catch {
                      return null;
                    }
                  })()}
                </div>
              )}
            </div>
          )}
          {deepAnalysis && (
            <div className="text-xs text-zinc-200 leading-relaxed bg-violet-500/10 border border-violet-500/20 rounded-lg p-2.5 space-y-1.5">
              <div className="flex items-center justify-between gap-2">
                <span className="text-brand-light font-semibold">Thinking review</span>
                <span className="text-[10px] text-zinc-400">
                  {deepAnalysis.verdict} · {deepAnalysis.confidence}%
                </span>
              </div>
              <p>{deepAnalysis.summary}</p>
              {deepAnalysis.bear_case.length > 0 && (
                <p className="text-zinc-300">
                  <span className="font-semibold">Watch: </span>{deepAnalysis.bear_case[0]}
                </p>
              )}
              {deepAnalysis.risk_controls.length > 0 && (
                <p className="text-zinc-400">
                  <span className="font-semibold">Control: </span>{deepAnalysis.risk_controls[0]}
                </p>
              )}
            </div>
          )}
          {thinkingError && (
            <div className="text-[10px] text-loss bg-red-500/10 rounded p-2">
              {thinkingError}
            </div>
          )}
          {signal.risk && (
            <div className="text-xs text-warn leading-relaxed bg-warn/5 rounded-lg p-2">
              <span className="font-semibold">⚠ Risk: </span>{signal.risk}
            </div>
          )}
          {(target != null || stop != null) && (
            <div className="space-y-1.5">
              <div className="grid grid-cols-3 gap-1.5 text-[10px]">
                <div className="bg-zinc-900/60 rounded p-1.5 text-center">
                  <div className="text-zinc-500">Entry</div>
                  <div className="text-zinc-100 font-semibold">₹{signal.current_price?.toFixed(1)}</div>
                </div>
                {target != null && (
                  <div className="bg-emerald-500/10 border border-emerald-500/20 rounded p-1.5 text-center">
                    <div className="text-zinc-500">Target</div>
                    <div className="text-emerald-400 font-semibold">₹{target.toFixed(1)}</div>
                  </div>
                )}
                {stop != null && (
                  <div className="bg-red-500/10 border border-red-500/20 rounded p-1.5 text-center">
                    <div className="text-zinc-500">Stop</div>
                    <div className="text-red-400 font-semibold">₹{stop.toFixed(1)}</div>
                  </div>
                )}
              </div>
              {qty != null && qty > 0 && (
                <div className="bg-zinc-900/40 rounded px-2 py-1.5 text-[10px] flex items-center justify-between">
                  <div>
                    <span className="text-zinc-500">Suggested qty </span>
                    <span className="font-bold text-zinc-100">{qty}</span>
                    {atr != null && (
                      <span className="text-zinc-600"> · ATR ₹{atr.toFixed(2)}</span>
                    )}
                  </div>
                  <div className="flex gap-2">
                    <span><span className="text-zinc-500">Risk </span><span className="text-loss font-medium">₹{Math.round(riskAmount ?? 0).toLocaleString("en-IN")}</span></span>
                    <span><span className="text-zinc-500">→ </span><span className="text-profit font-medium">₹{Math.round(rewardAmount ?? 0).toLocaleString("en-IN")}</span></span>
                    {rr != null && <span className="text-zinc-400">R:R {rr.toFixed(1)}</span>}
                  </div>
                </div>
              )}
              <div className="text-[10px] text-zinc-600">
                {source === "atr"
                  ? `Volatility-adjusted (ATR × ${(settings.atr_sl_mult ?? 1.5)} stop / ATR × ${(settings.atr_target_mult ?? 3)} target)`
                  : "Heuristic (ATR unavailable — using fixed % bands)"}
                {qty != null && qty > 0 && ` · ${(settings.risk_per_trade_pct ?? 1)}% of ₹${(settings.capital ?? 100000).toLocaleString("en-IN")} capital`}
              </div>
            </div>
          )}
          <MiniChart symbol={signal.symbol} height={120} />
          {muteMsg && <div className="text-[10px] text-zinc-400 italic">{muteMsg}</div>}
          {tradeAdded && <div className="text-[10px] text-profit">✓ Added to paper book</div>}
          <div className="flex items-center justify-between flex-wrap gap-1.5">
            <span className="text-xs text-zinc-500">Strength: {signal.strength}/10</span>
            <div className="flex gap-1.5 text-[10px]">
              {signal.current_price != null && (
                <button onClick={(e) => { e.stopPropagation(); takeTrade(); }}
                  className="px-2 py-0.5 rounded border border-emerald-500/30 text-emerald-400 hover:bg-emerald-500/10">
                  Take this trade
                </button>
              )}
              <button onClick={(e) => { e.stopPropagation(); runThinkingAnalysis(); }}
                disabled={thinking}
                className="px-2 py-0.5 rounded border border-violet-500/30 text-violet-300 hover:bg-violet-500/10 disabled:opacity-60">
                {thinking ? "Thinking..." : "Think"}
              </button>
              <button onClick={(e) => { e.stopPropagation(); muteSymbol(); }}
                className="px-2 py-0.5 rounded border border-zinc-700 text-zinc-400 hover:text-zinc-100">
                Mute {signal.symbol}
              </button>
              <button onClick={(e) => { e.stopPropagation(); muteType(); }}
                className="px-2 py-0.5 rounded border border-zinc-700 text-zinc-400 hover:text-zinc-100">
                Mute type
              </button>
              <button
                aria-label={`Dismiss ${signal.symbol} signal`}
                onClick={(e) => { e.stopPropagation(); onDismiss(signal.id); }}
                className="text-zinc-500 hover:text-loss px-1">
                Dismiss
              </button>
              <button
                aria-label="Ask agentX"
                onClick={(e) => { e.stopPropagation(); setChatOpen(true); }}
                className="px-2 py-0.5 rounded border border-emerald-700 text-emerald-300 hover:text-emerald-100">
                Ask agentX
              </button>
            </div>
          </div>
        </div>
      )}
      {chatOpen && (
        <SignalChatDrawer
          signalId={signal.id}
          onClose={() => setChatOpen(false)}
        />
      )}
    </div>
  );
}
