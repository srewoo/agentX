import { useState } from "react";
import { api } from "../../shared/api";

interface ScreenerResult {
  symbol: string;
  name: string;
  close: number;
  change_pct: number;
  rsi: number | null;
  volume_ratio: number | null;
  recommendation: string | null;
  dividend_yield: number | null;
}

const PRESETS = [
  { id: "oversold", label: "Oversold" },
  { id: "overbought", label: "Overbought" },
  { id: "volume_breakout", label: "Vol Breakout" },
  { id: "momentum", label: "Momentum" },
  { id: "dividend", label: "Dividend" },
] as const;

type PresetId = (typeof PRESETS)[number]["id"];

interface ScreenerProps {
  onSelectSymbol?: (symbol: string) => void;
}

export default function Screener({ onSelectSymbol }: ScreenerProps) {
  const [activePreset, setActivePreset] = useState<PresetId | null>(null);
  const [results, setResults] = useState<ScreenerResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const runPreset = async (preset: PresetId) => {
    setActivePreset(preset);
    setLoading(true);
    setError(null);
    setResults([]);
    try {
      const res = await api.screenerPreset(preset);
      setResults(res.results);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Screener request failed");
    } finally {
      setLoading(false);
    }
  };

  const handleRowClick = (symbol: string) => {
    if (onSelectSymbol) {
      onSelectSymbol(symbol);
    }
  };

  return (
    <div className="flex flex-col h-full">
      {/* Preset buttons */}
      <div className="px-3 pt-3 pb-2 border-b border-border">
        <div className="flex gap-1.5 flex-wrap">
          {PRESETS.map((preset) => (
            <button
              key={preset.id}
              onClick={() => runPreset(preset.id)}
              disabled={loading}
              className={`px-2.5 py-1.5 text-xs font-medium rounded-lg border transition-colors
                ${activePreset === preset.id
                  ? "bg-brand/20 text-brand-light border-brand/40"
                  : "bg-zinc-800 text-zinc-400 border-border hover:text-zinc-200 hover:border-zinc-600"
                } disabled:opacity-50`}
            >
              {preset.label}
            </button>
          ))}
        </div>
      </div>

      {/* Results */}
      <div className="flex-1 overflow-y-auto px-3 py-2">
        {error && (
          <div className="text-xs text-loss bg-loss/10 border border-loss/30 rounded p-2 mb-2">
            {error}
          </div>
        )}

        {loading && (
          <div className="flex flex-col items-center justify-center py-12 gap-2 text-zinc-500">
            <div className="w-5 h-5 border-2 border-zinc-600 border-t-brand rounded-full animate-spin" />
            <span className="text-xs">Running screener...</span>
          </div>
        )}

        {!loading && results.length > 0 && (
          <table role="table" className="w-full" aria-label="Screener results">
            {/* Table header */}
            <thead>
              <tr className="grid grid-cols-[1fr_56px_56px_44px_56px_72px] gap-1 px-2 py-1.5 text-[10px] font-medium text-zinc-500 uppercase tracking-wider border-b border-border">
                <th scope="col" className="text-left font-medium">Stock</th>
                <th scope="col" className="text-right font-medium">Price</th>
                <th scope="col" className="text-right font-medium">Chg%</th>
                <th scope="col" className="text-right font-medium">RSI</th>
                <th scope="col" className="text-right font-medium">{activePreset === "dividend" ? "Div %" : "Vol R"}</th>
                <th scope="col" className="text-right font-medium">Signal</th>
              </tr>
            </thead>

            <tbody>
            {/* Table rows */}
            {results.map((row) => {
              const isPos = row.change_pct >= 0;
              return (
                <tr
                  key={row.symbol}
                  role="row"
                  tabIndex={0}
                  onClick={() => handleRowClick(row.symbol)}
                  onKeyDown={(e) => { if (e.key === "Enter") handleRowClick(row.symbol); }}
                  className="grid grid-cols-[1fr_56px_56px_44px_56px_72px] gap-1 w-full text-left px-2 py-2 border-b border-border/50 hover:bg-zinc-800/60 transition-colors cursor-pointer"
                  aria-label={`${row.symbol} ${row.name}, price ${row.close.toFixed(1)}, change ${row.change_pct >= 0 ? "+" : ""}${row.change_pct.toFixed(2)}%`}
                >
                  <td className="min-w-0">
                    <div className="text-xs font-semibold text-zinc-100 truncate">{row.symbol}</div>
                    <div className="text-[10px] text-zinc-500 truncate">{row.name}</div>
                  </td>
                  <td className="text-xs text-zinc-200 text-right self-center font-medium">
                    {row.close.toFixed(1)}
                  </td>
                  <td className={`text-xs text-right self-center font-medium ${isPos ? "text-profit" : "text-loss"}`}>
                    <span aria-label={`${isPos ? "up" : "down"} ${Math.abs(row.change_pct).toFixed(2)} percent`}>
                      {isPos ? "+" : ""}{row.change_pct.toFixed(2)}%
                    </span>
                  </td>
                  <td className={`text-xs text-right self-center ${
                    row.rsi != null
                      ? row.rsi < 30 ? "text-profit" : row.rsi > 70 ? "text-loss" : "text-zinc-400"
                      : "text-zinc-600"
                  }`}>
                    {row.rsi != null ? row.rsi.toFixed(0) : "—"}
                  </td>
                  <td className="text-xs text-zinc-400 text-right self-center">
                    {activePreset === "dividend"
                      ? (row.dividend_yield != null ? <span className="text-emerald-400">{row.dividend_yield.toFixed(1)}%</span> : "—")
                      : (row.volume_ratio != null ? `${row.volume_ratio.toFixed(1)}x` : "—")}
                  </td>
                  <td className="text-right self-center">
                    <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium
                      ${row.recommendation === "BUY" || row.recommendation === "STRONG_BUY"
                        ? "bg-profit/15 text-profit"
                        : row.recommendation === "SELL" || row.recommendation === "STRONG_SELL"
                          ? "bg-loss/15 text-loss"
                          : "bg-zinc-700/50 text-zinc-400"
                      }`}>
                      {row.recommendation ?? "—"}
                    </span>
                  </td>
                </tr>
              );
            })}
            </tbody>
          </table>
        )}

        {!loading && !error && results.length === 0 && !activePreset && (
          <div className="flex flex-col items-center justify-center h-full gap-3 text-zinc-500">
            <span className="text-4xl">📊</span>
            <div className="text-center">
              <p className="text-sm font-medium text-zinc-400">Stock Screener</p>
              <p className="text-xs mt-1">Select a preset above to scan for stocks</p>
            </div>
          </div>
        )}

        {!loading && !error && results.length === 0 && activePreset && (
          <div className="flex flex-col items-center justify-center py-12 gap-2 text-zinc-500">
            <span className="text-2xl">📭</span>
            <p className="text-xs">No stocks matched the {activePreset} criteria</p>
          </div>
        )}
      </div>
    </div>
  );
}
