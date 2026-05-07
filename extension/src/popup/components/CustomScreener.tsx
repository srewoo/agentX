import { useEffect, useState } from "react";
import { api } from "../../shared/api";
import { customPresets } from "../../shared/localStore";
import type { ScreenerParams, CustomScreenerPreset } from "../../shared/types";

interface ScreenerResult {
  symbol: string;
  name: string;
  close: number;
  change_pct: number;
  rsi: number | null;
  volume_ratio: number | null;
  recommendation: string | null;
}

interface Props {
  onSelectSymbol?: (symbol: string) => void;
}

const SECTORS = [
  "", "Technology", "Financial Services", "Energy", "Materials",
  "Consumer Cyclical", "Consumer Defensive", "Healthcare", "Industrials",
  "Utilities", "Real Estate", "Communication Services",
];

export default function CustomScreener({ onSelectSymbol }: Props) {
  const [params, setParams] = useState<ScreenerParams>({ limit: 25 });
  const [results, setResults] = useState<ScreenerResult[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [presets, setPresets] = useState<CustomScreenerPreset[]>([]);
  const [presetName, setPresetName] = useState("");

  useEffect(() => {
    customPresets.list().then(setPresets);
  }, []);

  const set = (k: keyof ScreenerParams, v: unknown) => {
    setParams((p) => ({ ...p, [k]: v === "" || v == null ? undefined : v }));
  };

  const run = async () => {
    setLoading(true);
    setError(null);
    setResults(null);
    try {
      const r = await api.customScreener(params);
      setResults(r.results);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Screener failed");
    } finally {
      setLoading(false);
    }
  };

  const savePreset = async () => {
    if (!presetName.trim()) return;
    const p: CustomScreenerPreset = {
      id: crypto.randomUUID(),
      name: presetName.trim(),
      params: { ...params },
      created_at: new Date().toISOString(),
    };
    await customPresets.add(p);
    setPresets(await customPresets.list());
    setPresetName("");
  };

  const loadPreset = (p: CustomScreenerPreset) => {
    setParams({ ...p.params });
  };

  const removePreset = async (id: string) => {
    await customPresets.remove(id);
    setPresets(await customPresets.list());
  };

  const hasAnyFilter =
    params.rsi_min != null || params.rsi_max != null || params.volume_ratio_min != null ||
    params.change_pct_min != null || params.change_pct_max != null ||
    params.market_cap_min != null || params.market_cap_max != null || (params.sector ?? "") !== "";

  return (
    <div className="flex flex-col h-full">
      <div className="px-3 pt-2 pb-2 border-b border-border space-y-2">
        {presets.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {presets.map((p) => (
              <span key={p.id} className="inline-flex items-center bg-zinc-800 border border-border rounded-full pl-2 pr-1 py-0.5">
                <button onClick={() => loadPreset(p)} className="text-[10px] text-zinc-200 hover:text-brand-light">
                  {p.name}
                </button>
                <button onClick={() => removePreset(p.id)} className="text-zinc-600 hover:text-loss text-[10px] ml-1 px-0.5">×</button>
              </span>
            ))}
          </div>
        )}

        <div className="grid grid-cols-2 gap-2 text-[10px]">
          <div>
            <label className="text-zinc-500 block">RSI min</label>
            <input type="number" min={0} max={100} value={params.rsi_min ?? ""} onChange={(e) => set("rsi_min", e.target.value === "" ? undefined : Number(e.target.value))}
              className="w-full bg-zinc-800 border border-border rounded px-1.5 py-1 text-zinc-100" placeholder="—" />
          </div>
          <div>
            <label className="text-zinc-500 block">RSI max</label>
            <input type="number" min={0} max={100} value={params.rsi_max ?? ""} onChange={(e) => set("rsi_max", e.target.value === "" ? undefined : Number(e.target.value))}
              className="w-full bg-zinc-800 border border-border rounded px-1.5 py-1 text-zinc-100" placeholder="—" />
          </div>
          <div>
            <label className="text-zinc-500 block">Volume ratio ≥</label>
            <input type="number" step="0.1" value={params.volume_ratio_min ?? ""} onChange={(e) => set("volume_ratio_min", e.target.value === "" ? undefined : Number(e.target.value))}
              className="w-full bg-zinc-800 border border-border rounded px-1.5 py-1 text-zinc-100" placeholder="e.g. 2" />
          </div>
          <div>
            <label className="text-zinc-500 block">Change % range</label>
            <div className="flex gap-1">
              <input type="number" step="0.5" value={params.change_pct_min ?? ""} onChange={(e) => set("change_pct_min", e.target.value === "" ? undefined : Number(e.target.value))}
                className="w-full bg-zinc-800 border border-border rounded px-1.5 py-1 text-zinc-100" placeholder="min" />
              <input type="number" step="0.5" value={params.change_pct_max ?? ""} onChange={(e) => set("change_pct_max", e.target.value === "" ? undefined : Number(e.target.value))}
                className="w-full bg-zinc-800 border border-border rounded px-1.5 py-1 text-zinc-100" placeholder="max" />
            </div>
          </div>
          <div>
            <label className="text-zinc-500 block">Market cap (Cr) min</label>
            <input type="number" value={params.market_cap_min != null ? params.market_cap_min / 1e7 : ""} onChange={(e) => set("market_cap_min", e.target.value === "" ? undefined : Number(e.target.value) * 1e7)}
              className="w-full bg-zinc-800 border border-border rounded px-1.5 py-1 text-zinc-100" placeholder="e.g. 10000" />
          </div>
          <div>
            <label className="text-zinc-500 block">Sector</label>
            <select value={params.sector ?? ""} onChange={(e) => set("sector", e.target.value || undefined)}
              className="w-full bg-zinc-800 border border-border rounded px-1.5 py-1 text-zinc-100">
              {SECTORS.map((s) => <option key={s} value={s}>{s || "Any"}</option>)}
            </select>
          </div>
        </div>

        <div className="flex gap-1.5 pt-1">
          <input
            value={presetName}
            onChange={(e) => setPresetName(e.target.value)}
            placeholder="Preset name (optional)"
            className="flex-1 bg-zinc-800 border border-border rounded px-2 py-1 text-[11px] text-zinc-100 focus:outline-none focus:border-brand"
          />
          <button onClick={savePreset} disabled={!presetName.trim() || !hasAnyFilter}
            className="text-[10px] px-2 py-1 rounded bg-zinc-800 border border-border text-zinc-300 hover:text-zinc-100 disabled:opacity-40">
            Save
          </button>
          <button onClick={run} disabled={loading || !hasAnyFilter}
            className="text-[11px] px-3 py-1 rounded bg-brand text-white font-medium hover:bg-brand/80 disabled:opacity-50">
            {loading ? "…" : "Run"}
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-2">
        {error && <div className="text-xs text-loss bg-loss/10 border border-loss/30 rounded p-2 mb-2">{error}</div>}
        {results === null && !loading && !error && (
          <div className="text-xs text-zinc-500 text-center py-8">
            Set at least one filter and click Run.
          </div>
        )}
        {results && results.length === 0 && (
          <div className="text-xs text-zinc-500 text-center py-8">No matches.</div>
        )}
        {results && results.map((r) => {
          const isPos = r.change_pct >= 0;
          return (
            <button
              key={r.symbol}
              onClick={() => onSelectSymbol?.(r.symbol)}
              className="w-full grid grid-cols-[1fr_56px_56px_44px_56px_72px] gap-1 text-left px-2 py-1.5 border-b border-border/50 hover:bg-zinc-800/60 rounded"
            >
              <div className="min-w-0">
                <div className="text-xs font-semibold text-zinc-100 truncate">{r.symbol}</div>
                <div className="text-[10px] text-zinc-500 truncate">{r.name}</div>
              </div>
              <div className="text-xs text-zinc-200 text-right self-center">{r.close.toFixed(1)}</div>
              <div className={`text-xs text-right self-center font-medium ${isPos ? "text-profit" : "text-loss"}`}>
                {isPos ? "+" : ""}{r.change_pct.toFixed(2)}%
              </div>
              <div className={`text-xs text-right self-center ${
                r.rsi != null ? (r.rsi < 30 ? "text-profit" : r.rsi > 70 ? "text-loss" : "text-zinc-400") : "text-zinc-600"
              }`}>{r.rsi != null ? r.rsi.toFixed(0) : "—"}</div>
              <div className="text-xs text-zinc-400 text-right self-center">{r.volume_ratio != null ? `${r.volume_ratio.toFixed(1)}x` : "—"}</div>
              <div className="text-right self-center">
                <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
                  r.recommendation === "BUY" || r.recommendation === "STRONG_BUY" ? "bg-profit/15 text-profit"
                    : r.recommendation === "SELL" || r.recommendation === "STRONG_SELL" ? "bg-loss/15 text-loss"
                    : "bg-zinc-700/50 text-zinc-400"}`}>
                  {r.recommendation ?? "—"}
                </span>
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
