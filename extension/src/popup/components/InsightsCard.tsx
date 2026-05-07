import { useEffect, useState } from "react";
import { api } from "../../shared/api";
import { getSettings, saveSettings } from "../../shared/storage";
import type { AppSettings, Insight } from "../../shared/types";

/**
 * Surfaces actionable insights from /api/performance/insights — the
 * autonomous feedback loop's recommendations to the user.
 *
 * Shows up to 3 insights ranked by severity. One-click apply for
 * mute / apply_mutes actions. Hidden entirely when there's nothing
 * meaningful to say (cold install).
 */

const SEV_STYLE: Record<string, string> = {
  warn: "bg-amber-500/10 border-amber-500/30 text-amber-200",
  good: "bg-emerald-500/10 border-emerald-500/30 text-emerald-200",
  info: "bg-zinc-800/60 border-zinc-700 text-zinc-300",
};

const SEV_ICON: Record<string, string> = {
  warn: "⚠",
  good: "✓",
  info: "ℹ",
};

export default function InsightsCard() {
  const [insights, setInsights] = useState<Insight[]>([]);
  const [collapsed, setCollapsed] = useState(false);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [appliedKey, setAppliedKey] = useState<string | null>(null);

  useEffect(() => {
    api.getInsights()
      .then((r) => setInsights(r.insights || []))
      .catch(() => setInsights([]));
  }, []);

  const muteOne = async (signalType: string) => {
    setBusyAction(`mute:${signalType}`);
    const s = (await getSettings()) as Partial<AppSettings>;
    const set = new Set(s.muted_signal_types ?? []);
    set.add(signalType);
    await saveSettings({ ...s, muted_signal_types: Array.from(set) });
    setAppliedKey(`mute:${signalType}`);
    setBusyAction(null);
  };

  const applyAllMutes = async (types: string[]) => {
    setBusyAction("apply_mutes");
    const s = (await getSettings()) as Partial<AppSettings>;
    const set = new Set(s.muted_signal_types ?? []);
    types.forEach((t) => set.add(t));
    await saveSettings({ ...s, muted_signal_types: Array.from(set) });
    setAppliedKey("apply_mutes");
    setBusyAction(null);
  };

  if (!insights.length) return null;

  const topThree = insights.slice(0, 3);
  const warnCount = insights.filter((i) => i.severity === "warn").length;

  return (
    <div className="border-b border-border bg-zinc-900/30">
      <button
        onClick={() => setCollapsed((v) => !v)}
        className="w-full flex items-center justify-between px-3 py-1.5 text-[10px] text-zinc-400 uppercase tracking-wider hover:text-zinc-200"
      >
        <span>
          🧠 Agent Insights {warnCount > 0 && (
            <span className="ml-1 inline-block bg-amber-500/20 text-amber-400 border border-amber-500/30 rounded-full px-1.5 py-0.5 text-[9px] normal-case font-semibold">
              {warnCount} action needed
            </span>
          )}
        </span>
        <span className="text-zinc-600">{collapsed ? "▼" : "▲"}</span>
      </button>
      {!collapsed && (
        <div className="px-3 pb-2 space-y-1.5">
          {topThree.map((ins, i) => {
            const key = ins.kind === "drift" && ins.signal_type
              ? `drift:${ins.signal_type}:${ins.direction ?? ""}`
              : ins.kind === "recommended_mutes"
                ? "apply_mutes"
                : `wow:${i}`;
            const applied = appliedKey === (ins.action === "mute" ? `mute:${ins.signal_type}` : ins.action === "apply_mutes" ? "apply_mutes" : null);
            return (
              <div
                key={key}
                className={`rounded border px-2 py-1.5 text-[11px] leading-snug ${SEV_STYLE[ins.severity] ?? SEV_STYLE.info}`}
              >
                <div className="flex items-start gap-1.5">
                  <span className="flex-shrink-0">{SEV_ICON[ins.severity] ?? "•"}</span>
                  <div className="min-w-0 flex-1">
                    <div className="font-medium">{ins.title}</div>
                    {ins.kind === "drift" && (
                      <div className="text-[10px] opacity-80 mt-0.5">
                        Live {ins.live_win_rate?.toFixed(1)}% vs baseline {ins.baseline_win_rate?.toFixed(1)}% · n={ins.sample_size}
                      </div>
                    )}
                    {ins.kind === "wow" && ins.current && (
                      <div className="text-[10px] opacity-80 mt-0.5">
                        WR {ins.current.wr?.toFixed(1)}% (was {ins.previous?.wr?.toFixed(1)}%) ·
                        avg {ins.current.pnl != null && ins.current.pnl >= 0 ? "+" : ""}{ins.current.pnl?.toFixed(2)}%
                      </div>
                    )}
                    {ins.kind === "recommended_mutes" && ins.signal_types && (
                      <div className="text-[10px] opacity-80 mt-0.5">
                        {ins.signal_types.join(", ")}
                      </div>
                    )}
                  </div>
                  {ins.action === "mute" && ins.signal_type && !applied && (
                    <button
                      onClick={() => muteOne(ins.signal_type!)}
                      disabled={busyAction === `mute:${ins.signal_type}`}
                      className="text-[10px] px-2 py-0.5 rounded border border-current opacity-90 hover:opacity-100 flex-shrink-0"
                    >
                      {busyAction === `mute:${ins.signal_type}` ? "…" : ins.action_label ?? "Mute"}
                    </button>
                  )}
                  {ins.action === "apply_mutes" && ins.signal_types && !applied && (
                    <button
                      onClick={() => applyAllMutes(ins.signal_types!)}
                      disabled={busyAction === "apply_mutes"}
                      className="text-[10px] px-2 py-0.5 rounded border border-current opacity-90 hover:opacity-100 flex-shrink-0"
                    >
                      {busyAction === "apply_mutes" ? "…" : ins.action_label ?? "Apply"}
                    </button>
                  )}
                  {applied && (
                    <span className="text-[10px] text-emerald-400 flex-shrink-0">✓ applied</span>
                  )}
                </div>
              </div>
            );
          })}
          {insights.length > 3 && (
            <div className="text-[10px] text-zinc-500 text-center pt-0.5">
              +{insights.length - 3} more insight{insights.length - 3 > 1 ? "s" : ""}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
