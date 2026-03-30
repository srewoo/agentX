import type { AIAnalysisResponse } from "../../shared/types";
import { Disclaimer } from "./Disclaimer";

interface Props {
  result: AIAnalysisResponse;
}

const STANCE_CONFIG: Record<string, { color: string; bg: string; border: string; label: string }> = {
  BUY:          { color: "#10B981", bg: "bg-emerald-500/15", border: "border-emerald-500/40", label: "BUY" },
  CAUTIOUS_BUY: { color: "#10B981", bg: "bg-emerald-500/10", border: "border-emerald-500/30", label: "Cautious Buy" },
  SELL:         { color: "#EF4444", bg: "bg-red-500/15",     border: "border-red-500/40",     label: "SELL" },
  CAUTIOUS_SELL:{ color: "#EF4444", bg: "bg-red-500/10",     border: "border-red-500/30",     label: "Cautious Sell" },
  HOLD:         { color: "#F59E0B", bg: "bg-amber-500/15",   border: "border-amber-500/40",   label: "HOLD" },
};

const TIMEFRAME_LABELS: Record<string, string> = {
  intraday: "Intraday (today)",
  swing: "Swing (1–2 weeks)",
  long: "Long-term (3–12 months)",
  short: "Short-term",
};

export default function AnalysisPanel({ result }: Props) {
  const { analysis } = result;
  const cfg = STANCE_CONFIG[analysis.stance] ?? STANCE_CONFIG.HOLD;
  const timeframeLabel = TIMEFRAME_LABELS[result.timeframe] ?? result.timeframe;

  return (
    <div className="bg-panel rounded-xl border border-border p-4 space-y-3">
      {/* Stance + Confidence header */}
      <div className="flex items-center justify-between">
        <div>
          <span className="text-xs text-zinc-500">AI Analysis · {timeframeLabel}</span>
          <div className="flex items-center gap-2 mt-1">
            <span
              className={`text-xl font-bold px-3 py-0.5 rounded-lg border ${cfg.bg} ${cfg.border}`}
              style={{ color: cfg.color }}
            >
              {cfg.label}
            </span>
          </div>
        </div>
        <div className="text-right">
          <div className="text-xs text-zinc-500 mb-1">Confidence</div>
          <div className="relative w-12 h-12" role="meter" aria-valuenow={analysis.confidence} aria-valuemin={0} aria-valuemax={100} aria-label={`Confidence: ${analysis.confidence}%`}>
            <svg className="w-12 h-12 -rotate-90" viewBox="0 0 36 36" aria-hidden="true">
              <circle cx="18" cy="18" r="15" fill="none" stroke="#3F3F46" strokeWidth="3" />
              <circle
                cx="18" cy="18" r="15" fill="none"
                stroke={cfg.color}
                strokeWidth="3"
                strokeDasharray={`${(analysis.confidence / 100) * 94.2} 94.2`}
                strokeLinecap="round"
              />
            </svg>
            <span className="absolute inset-0 flex items-center justify-center text-xs font-bold text-zinc-200" aria-hidden="true">
              {analysis.confidence}%
            </span>
          </div>
        </div>
      </div>

      {/* Sentiment pill */}
      <div className="flex gap-2">
        <span className={`text-xs px-2 py-0.5 rounded-full border font-medium
          ${analysis.sentiment === "Bullish" ? "border-emerald-500/40 text-emerald-400 bg-emerald-500/10"
            : analysis.sentiment === "Bearish" ? "border-red-500/40 text-red-400 bg-red-500/10"
            : "border-amber-500/40 text-amber-400 bg-amber-500/10"}`}>
          {analysis.sentiment}
        </span>
      </div>

      {/* Summary */}
      <p className="text-xs text-zinc-300 leading-relaxed">{analysis.summary}</p>

      {/* Technical outlook */}
      {analysis.technical_outlook && (
        <div className="bg-zinc-900/60 rounded-lg p-2.5">
          <span className="text-xs text-brand-light font-semibold">Technical: </span>
          <span className="text-xs text-zinc-300">{analysis.technical_outlook}</span>
        </div>
      )}

      {/* Key reasons + Risks side by side */}
      <div className="grid grid-cols-2 gap-3">
        {analysis.key_reasons?.length > 0 && (
          <div>
            <div className="text-xs text-zinc-500 mb-1.5 font-semibold">✓ Supporting</div>
            <ul className="space-y-1">
              {analysis.key_reasons.map((r, i) => (
                <li key={i} className="text-xs text-zinc-300 flex gap-1.5">
                  <span className="text-emerald-400 flex-shrink-0">•</span>
                  <span>{r}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        {analysis.risks?.length > 0 && (
          <div>
            <div className="text-xs text-zinc-500 mb-1.5 font-semibold">⚠ Risks</div>
            <ul className="space-y-1">
              {analysis.risks.map((r, i) => (
                <li key={i} className="text-xs text-zinc-300 flex gap-1.5">
                  <span className="text-amber-400 flex-shrink-0">•</span>
                  <span>{r}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {/* S/R zones */}
      <div className="grid grid-cols-2 gap-2">
        {analysis.support_zone && (
          <div className="bg-emerald-500/10 border border-emerald-500/20 rounded-lg p-2 text-center">
            <div className="text-[10px] text-zinc-500 mb-0.5">Support Zone</div>
            <div className="text-xs font-semibold text-emerald-400">{analysis.support_zone}</div>
          </div>
        )}
        {analysis.resistance_zone && (
          <div className="bg-red-500/10 border border-red-500/20 rounded-lg p-2 text-center">
            <div className="text-[10px] text-zinc-500 mb-0.5">Resistance Zone</div>
            <div className="text-xs font-semibold text-red-400">{analysis.resistance_zone}</div>
          </div>
        )}
      </div>

      <Disclaimer />
    </div>
  );
}
