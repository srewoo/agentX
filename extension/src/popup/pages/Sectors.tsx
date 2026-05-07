import { useEffect, useMemo, useState } from "react";
import { api } from "../../shared/api";

interface IndexRow { symbol: string; price: number; change: number; change_pct: number; }

const BROAD = [
  "NIFTY 50", "NIFTY NEXT 50", "NIFTY 100", "NIFTY 200", "NIFTY 500",
  "NIFTY MIDCAP 100", "NIFTY MIDCAP 50", "NIFTY MIDCAP 150",
  "NIFTY SMLCAP 100", "NIFTY SMALLCAP 100", "NIFTY SMALLCAP 250",
  "BSE SENSEX", "INDIA VIX",
];

function isSectoral(name: string): boolean {
  if (BROAD.includes(name)) return false;
  // Match any NIFTY sectoral or thematic index — covers BANK, AUTO, IT, PHARMA,
  // FMCG, METAL, REALTY, MEDIA, ENERGY, FIN SERVICES, PSU BANK, PRIVATE BANK,
  // INFRA, CONSUMER DURABLES, HEALTHCARE, OIL & GAS, COMMODITIES, CPSE, etc.
  return /^NIFTY\s+(BANK|AUTO|IT|PHARMA|FMCG|METAL|REALTY|MEDIA|ENERGY|FINANCIAL|FIN |PSU|PRIVATE|CONSUMER|INFRA|COMMODITIES|CPSE|HEALTHCARE|OIL|GAS)/i.test(name);
}

/** Shorten "NIFTY MIDCAP 100" → "MIDCAP 100", "NIFTY 50" → "NIFTY 50" (keep). */
function shortLabel(name: string): string {
  if (name === "NIFTY 50" || name === "NIFTY NEXT 50" || name === "INDIA VIX" || name === "BSE SENSEX") return name;
  return name.replace(/^NIFTY\s+/, "");
}

function colorFor(pct: number): string {
  // tailwind doesn't allow dynamic class names; use inline style for the heatmap
  if (pct >= 2) return "rgba(16,185,129,0.55)";
  if (pct >= 1) return "rgba(16,185,129,0.35)";
  if (pct >= 0.2) return "rgba(16,185,129,0.20)";
  if (pct > -0.2) return "rgba(63,63,70,0.55)";
  if (pct > -1) return "rgba(239,68,68,0.20)";
  if (pct > -2) return "rgba(239,68,68,0.35)";
  return "rgba(239,68,68,0.55)";
}

export default function Sectors() {
  const [indices, setIndices] = useState<Record<string, IndexRow> | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.getIndices()
      .then((r) => setIndices(r as Record<string, IndexRow>))
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load"));
  }, []);

  const { broad, sectors } = useMemo(() => {
    const all = indices ? Object.entries(indices) : [];
    return {
      broad: all.filter(([k]) => BROAD.includes(k)).sort(([a], [b]) => BROAD.indexOf(a) - BROAD.indexOf(b)),
      sectors: all
        .filter(([k]) => isSectoral(k))
        .sort((a, b) => (b[1].change_pct ?? 0) - (a[1].change_pct ?? 0)),
    };
  }, [indices]);

  return (
    <div className="flex flex-col h-full">
      <div className="flex-1 overflow-y-auto px-3 py-2 space-y-3">
        {error && <div className="text-xs text-loss bg-loss/10 border border-loss/30 rounded p-2">{error}</div>}
        {!error && indices === null && <div className="text-xs text-zinc-500 text-center py-6">Loading indices…</div>}
        {indices !== null && (
          <>
            {broad.length > 0 && (
              <div>
                <div className="text-[10px] font-semibold text-zinc-500 uppercase tracking-wider mb-1.5">Broad market</div>
                <div className="grid grid-cols-3 gap-1.5">
                  {broad.map(([k, v]) => (
                    <div
                      key={k}
                      className="rounded-lg border border-zinc-700/50 px-2 py-1.5"
                      style={{ backgroundColor: colorFor(v.change_pct ?? 0) }}
                      title={`${k} ${v.price?.toLocaleString("en-IN")} (${v.change_pct?.toFixed(2)}%)`}
                    >
                      <div className="text-[10px] text-zinc-200 font-semibold truncate">{shortLabel(k)}</div>
                      <div className="text-[10px] text-zinc-300">{v.price?.toLocaleString("en-IN", { maximumFractionDigits: 0 })}</div>
                      <div className={`text-[10px] font-medium ${(v.change_pct ?? 0) >= 0 ? "text-emerald-300" : "text-red-300"}`}>
                        {(v.change_pct ?? 0) >= 0 ? "+" : ""}{v.change_pct?.toFixed(2)}%
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {sectors.length > 0 && (
              <div>
                <div className="flex items-center justify-between mb-1.5">
                  <div className="text-[10px] font-semibold text-zinc-500 uppercase tracking-wider">Sectoral heatmap</div>
                  <div className="text-[10px] text-zinc-600">sorted by % change</div>
                </div>
                <div className="grid grid-cols-2 gap-1.5">
                  {sectors.map(([k, v]) => (
                    <div
                      key={k}
                      className="rounded-lg border border-zinc-700/40 px-2 py-1.5"
                      style={{ backgroundColor: colorFor(v.change_pct ?? 0) }}
                    >
                      <div className="text-[10px] text-zinc-200 font-semibold truncate">{shortLabel(k)}</div>
                      <div className="flex items-center justify-between">
                        <span className="text-[10px] text-zinc-300">{v.price?.toLocaleString("en-IN", { maximumFractionDigits: 0 })}</span>
                        <span className={`text-[10px] font-bold ${(v.change_pct ?? 0) >= 0 ? "text-emerald-200" : "text-red-200"}`}>
                          {(v.change_pct ?? 0) >= 0 ? "+" : ""}{v.change_pct?.toFixed(2)}%
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {sectors.length === 0 && broad.length === 0 && (
              <div className="text-xs text-zinc-500 text-center py-8">No index data available.</div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
