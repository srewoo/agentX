import { useState } from "react";
import type { Signal } from "../../shared/types";
import { SIGNAL_TYPE_LABELS, DIRECTION_ACTION, ACTION_COLORS, getSignalTimeframe } from "../../shared/constants";
import { api } from "../../shared/api";
import MiniChart from "./MiniChart";

interface Props {
  signal: Signal;
  onRead: (id: string) => void;
  onDismiss: (id: string) => void;
}

function timeAgo(isoDate: string): string {
  const diff = Date.now() - new Date(isoDate).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

// Use shared getSignalTimeframe from constants to avoid duplication

const TIMEFRAME_STYLE: Record<string, string> = {
  Intraday: "bg-blue-500/15 text-blue-400 border-blue-500/30",
  Swing: "bg-purple-500/15 text-purple-400 border-purple-500/30",
  "Long-term": "bg-amber-500/15 text-amber-400 border-amber-500/30",
};

export default function SignalCard({ signal, onRead, onDismiss }: Props) {
  const [expanded, setExpanded] = useState(false);

  const action = DIRECTION_ACTION[signal.direction] || "HOLD";
  const actionColor = ACTION_COLORS[action] || "#F59E0B";
  const timeframe = getSignalTimeframe(signal.signal_type, signal.strength);
  const label = SIGNAL_TYPE_LABELS[signal.signal_type] || signal.signal_type;

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
      {/* Row 1: Symbol + BUY/SELL/HOLD badge + time */}
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2.5">
          {!signal.read && (
            <span className="w-2 h-2 rounded-full bg-brand-light flex-shrink-0" aria-hidden="true" />
          )}
          <span className="font-bold text-base text-zinc-100">{signal.symbol}</span>

          {/* BUY / SELL / HOLD badge */}
          <span
            className="text-xs font-bold px-2 py-0.5 rounded-md border"
            style={{
              color: actionColor,
              borderColor: `${actionColor}40`,
              backgroundColor: `${actionColor}15`,
            }}
          >
            {action}
          </span>

          {/* Timeframe tag */}
          <span className={`text-[11px] font-medium px-1.5 py-0.5 rounded border ${TIMEFRAME_STYLE[timeframe]}`}>
            {timeframe}
          </span>
        </div>

        <div className="flex items-center gap-2">
          <span className="text-xs text-zinc-500">{timeAgo(signal.created_at)}</span>
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
          {signal.metadata?.conflicting_signals && (
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
          {signal.llm_summary && (
            <div className="text-xs text-zinc-200 leading-relaxed bg-zinc-900/60 rounded-lg p-2.5">
              <span className="text-brand-light font-semibold">AI Insight: </span>
              {signal.llm_summary}
            </div>
          )}
          {signal.risk && (
            <div className="text-xs text-warn leading-relaxed bg-warn/5 rounded-lg p-2">
              <span className="font-semibold">⚠ Risk: </span>{signal.risk}
            </div>
          )}
          <MiniChart symbol={signal.symbol} height={120} />
          <div className="flex items-center justify-between">
            <span className="text-xs text-zinc-500">Strength: {signal.strength}/10</span>
            <button
              className="text-xs text-zinc-500 hover:text-loss"
              aria-label={`Dismiss ${signal.symbol} signal`}
              onClick={(e) => { e.stopPropagation(); onDismiss(signal.id); }}
            >
              Dismiss
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
