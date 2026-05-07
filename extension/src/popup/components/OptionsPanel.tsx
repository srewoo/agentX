import { useEffect, useState } from "react";
import { api } from "../../shared/api";
import type { OptionsAnalysis } from "../../shared/types";

interface Props { symbol: string; }

function pcrSentiment(pcr?: number): { label: string; cls: string } {
  if (pcr == null) return { label: "—", cls: "text-zinc-500" };
  if (pcr > 1.3) return { label: "Bullish (oversold puts)", cls: "text-profit" };
  if (pcr < 0.7) return { label: "Bearish (overbought calls)", cls: "text-loss" };
  return { label: "Neutral", cls: "text-amber-400" };
}

/**
 * Lazy options chain panel. Stays collapsed (and silent on the network) until
 * the user expands it — prevents an option-chain call per Search symbol pick.
 * Backend caches per-symbol for 5 min, so re-expansion is cheap.
 */
export default function OptionsPanel({ symbol }: Props) {
  const [data, setData] = useState<OptionsAnalysis | null>(null);
  const [loading, setLoading] = useState(false);
  const [collapsed, setCollapsed] = useState(true);

  // Reset state when symbol changes; never fetch eagerly.
  useEffect(() => {
    setData(null);
    setCollapsed(true);
    setLoading(false);
  }, [symbol]);

  const expand = () => {
    if (collapsed) {
      setCollapsed(false);
      if (!data && !loading) {
        setLoading(true);
        api.getOptionsAnalysis(symbol)
          .then(setData)
          .catch(() => setData({ symbol, error: "Failed to fetch options data" }))
          .finally(() => setLoading(false));
      }
    } else {
      setCollapsed(true);
    }
  };

  const pcr = pcrSentiment(data?.pcr);
  const unusual = data?.unusual_oi || [];

  return (
    <div className="bg-panel rounded-xl border border-border overflow-hidden">
      <button
        onClick={expand}
        className="w-full flex items-center justify-between px-3 py-2 text-[11px] font-semibold text-zinc-300 hover:bg-zinc-800/50"
      >
        <span>📈 Options flow{data?.expiry ? ` · exp ${data.expiry}` : ""}</span>
        <span className="text-zinc-500">{collapsed ? "▼" : "▲"}</span>
      </button>
      {!collapsed && (
        <div className="px-3 py-2 space-y-2">
          {loading && <div className="text-[11px] text-zinc-500 text-center py-2">Loading options chain…</div>}
          {!loading && (!data || data.error) && (
            <div className="text-[11px] text-zinc-500 py-1">
              {data?.error || "No options data — likely not FnO-eligible."}
            </div>
          )}
          {!loading && data && !data.error && (
            <>
              <div className="grid grid-cols-3 gap-2 text-center">
                <div className="bg-zinc-900/60 rounded-lg p-1.5">
                  <div className="text-[10px] text-zinc-500">PCR</div>
                  <div className="text-sm font-bold text-zinc-100">{data.pcr?.toFixed(2) ?? "—"}</div>
                  <div className={`text-[9px] mt-0.5 ${pcr.cls}`}>{pcr.label}</div>
                </div>
                <div className="bg-zinc-900/60 rounded-lg p-1.5">
                  <div className="text-[10px] text-zinc-500">Max pain</div>
                  <div className="text-sm font-bold text-zinc-100">{data.max_pain != null ? `₹${data.max_pain.toLocaleString("en-IN")}` : "—"}</div>
                  <div className="text-[9px] mt-0.5 text-zinc-500">strike</div>
                </div>
                <div className="bg-zinc-900/60 rounded-lg p-1.5">
                  <div className="text-[10px] text-zinc-500">Spot</div>
                  <div className="text-sm font-bold text-zinc-100">{data.spot != null ? `₹${data.spot.toLocaleString("en-IN")}` : "—"}</div>
                  <div className="text-[9px] mt-0.5 text-zinc-500">vs max pain</div>
                </div>
              </div>
              {unusual.length > 0 && (
                <div>
                  <div className="text-[10px] text-zinc-500 mb-1">Unusual OI build-up</div>
                  <div className="space-y-0.5">
                    {unusual.slice(0, 5).map((u, i) => (
                      <div key={i} className="flex items-center justify-between text-[11px] py-0.5 px-1.5 rounded bg-zinc-900/40">
                        <span className={u.type === "CE" ? "text-emerald-400 font-semibold" : "text-red-400 font-semibold"}>
                          {u.strike} {u.type}
                        </span>
                        <span className="text-zinc-400">
                          OI {u.oi?.toLocaleString("en-IN")}{" "}
                          <span className={u.change_oi > 0 ? "text-emerald-400" : "text-red-400"}>
                            ({u.change_oi > 0 ? "+" : ""}{u.change_oi?.toLocaleString("en-IN")})
                          </span>
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {data.sentiment && <div className="text-[11px] text-zinc-400 italic">{data.sentiment}</div>}
            </>
          )}
        </div>
      )}
    </div>
  );
}
