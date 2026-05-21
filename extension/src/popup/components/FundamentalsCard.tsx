import { useEffect, useState } from "react";
import { api } from "../../shared/api";
import { useExchange } from "../lib/ExchangeContext";
import type { FundamentalsResponse } from "../../shared/types";

interface Props { symbol: string; }

/**
 * Compact fundamentals snapshot. Lazy-loaded — does nothing on the network
 * until expanded, mirroring OptionsPanel/BacktestPanel for rate-limit hygiene.
 *
 * When the backend supplies sector medians, every metric is rendered as
 * "value vs sector (±%)" — this is what an analyst actually reads. We fall
 * back to absolute bands only when no sector median is available.
 */

/**
 * "Higher-is-better" metrics (ROE, growth, margins) get green when above the
 * sector median by ≥10%. "Lower-is-better" metrics (PE, P/B, EV/EBITDA, D/E)
 * get green when below the sector median by ≥10%. The 10% deadband prevents
 * noisy colour flipping for tiny diffs.
 */
function diffColour(value: number | null | undefined, median: number | null | undefined, higherBetter: boolean): string {
  if (value == null || median == null || isNaN(value) || isNaN(median) || median === 0) return "text-zinc-300";
  const ratio = value / median;
  const upGood = higherBetter ? ratio > 1.10 : ratio < 0.90;
  const downBad = higherBetter ? ratio < 0.90 : ratio > 1.10;
  if (upGood) return "text-profit";
  if (downBad) return "text-loss";
  return "text-zinc-300";
}

function fmtVsMedian(value: number | null | undefined, median: number | null | undefined, asPct = false): string {
  if (value == null || median == null || median === 0 || isNaN(value) || isNaN(median)) return "";
  const v = asPct && Math.abs(value) < 1.5 ? value * 100 : value;
  const m = asPct && Math.abs(median) < 1.5 ? median * 100 : median;
  const delta = ((v - m) / m) * 100;
  const sign = delta >= 0 ? "+" : "";
  return `${sign}${delta.toFixed(0)}% vs ${m.toFixed(asPct ? 1 : 1)}${asPct ? "%" : ""}`;
}

function band(value: number | null | undefined, good: (v: number) => boolean, bad: (v: number) => boolean): string {
  if (value == null || isNaN(value)) return "text-zinc-400";
  if (good(value)) return "text-profit";
  if (bad(value)) return "text-loss";
  return "text-zinc-300";
}

function fmtPct(v: number | null | undefined, digits = 1): string {
  if (v == null) return "—";
  // yfinance returns 0.18 for 18% in some fields, raw % in others. Normalize:
  const pct = Math.abs(v) > 1.5 ? v : v * 100;
  return `${pct.toFixed(digits)}%`;
}

function fmtNum(v: number | null | undefined, digits = 1): string {
  if (v == null) return "—";
  return v.toFixed(digits);
}

function fmtCr(v: number | null | undefined): string {
  if (v == null) return "—";
  if (Math.abs(v) >= 1e9) return `₹${(v / 1e9).toFixed(1)}KCr`;
  if (Math.abs(v) >= 1e7) return `₹${(v / 1e7).toFixed(0)}Cr`;
  return `₹${v.toLocaleString("en-IN")}`;
}

export default function FundamentalsCard({ symbol }: Props) {
  const exchange = useExchange();
  const [data, setData] = useState<FundamentalsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(true);

  useEffect(() => {
    setData(null);
    setError(null);
    setCollapsed(true);
    setLoading(false);
  }, [symbol]);

  const expand = () => {
    if (collapsed) {
      setCollapsed(false);
      if (!data && !loading) {
        setLoading(true);
        api.getFundamentals(symbol, exchange)
          .then((d) => {
            // Backend returns 200 with an `error` field when yfinance is
            // rate-limited or returned an empty info dict — surface that
            // as a visible message instead of a wall of em-dashes.
            const err = (d as { error?: string })?.error;
            if (err) setError(err);
            else setData(d);
          })
          .catch((e) => setError(e instanceof Error ? e.message : "Fundamentals unavailable"))
          .finally(() => setLoading(false));
      }
    } else {
      setCollapsed(true);
    }
  };

  return (
    <div className="bg-panel rounded-xl border border-border overflow-hidden">
      <button
        onClick={expand}
        className="w-full flex items-center justify-between px-3 py-2 text-[11px] font-semibold text-zinc-300 hover:bg-zinc-800/50"
      >
        <span>
          🏛️ Fundamentals
          {data?.health_score != null && (
            <span className={`ml-2 text-[10px] px-1.5 py-0.5 rounded border ${
              data.health_score >= 7 ? "bg-emerald-500/15 text-emerald-400 border-emerald-500/30"
                : data.health_score >= 5 ? "bg-amber-500/15 text-amber-400 border-amber-500/30"
                : "bg-red-500/15 text-red-400 border-red-500/30"
            }`}>
              Health {data.health_score.toFixed(1)}/10 · {data.signal ?? ""}
            </span>
          )}
        </span>
        <span className="text-zinc-500">{collapsed ? "▼" : "▲"}</span>
      </button>
      {!collapsed && (
        <div className="px-3 py-2 space-y-2">
          {loading && <div className="text-[11px] text-zinc-500 text-center py-2">Loading fundamentals…</div>}
          {error && <div className="text-[11px] text-loss">{error}</div>}
          {!loading && !error && data && (
            <>
              {(data.sector || data.industry) && (
                <div className="text-[11px] text-zinc-400">
                  <span className="text-zinc-500">Sector: </span>{data.sector ?? "—"}
                  {data.industry && <> · <span className="text-zinc-500">Industry: </span>{data.industry}</>}
                </div>
              )}

              {(() => {
                const m = data.sector_medians;
                // Cell renderer: when sector median is known, colour by diff and
                // append the "+12% vs 22.5" line. Else fall back to absolute bands.
                const cell = (
                  label: string,
                  value: number | null | undefined,
                  median: number | null | undefined,
                  higherBetter: boolean,
                  fallbackBand: string,
                  fmt: (v: number | null | undefined) => string,
                  pctMedian = false,
                ) => (
                  <div className="bg-zinc-900/60 rounded p-1.5">
                    <div className="text-zinc-500">{label}</div>
                    <div className={`font-semibold ${m && median != null ? diffColour(value, median, higherBetter) : fallbackBand}`}>
                      {fmt(value)}
                    </div>
                    {m && median != null && value != null && (
                      <div className="text-[9px] text-zinc-500">{fmtVsMedian(value, median, pctMedian)}</div>
                    )}
                  </div>
                );

                return (
                  <div className="grid grid-cols-3 gap-1.5 text-[10px]">
                    {cell("PE", data.valuation?.pe, m?.pe, false,
                      band(data.valuation?.pe, v => v > 0 && v < 18, v => v > 60 || v <= 0),
                      fmtNum)}
                    {cell("P/B", data.valuation?.pb, m?.pb, false,
                      band(data.valuation?.pb, v => v > 0 && v < 3, v => v > 8 || v <= 0),
                      fmtNum)}
                    {cell("EV/EBITDA", data.valuation?.ev_ebitda, m?.ev_ebitda, false,
                      "text-zinc-300", fmtNum)}

                    {cell("ROE", data.profitability?.roe, m?.roe, true,
                      band(data.profitability?.roe, v => v > 0.15, v => v < 0.05),
                      fmtPct, true)}
                    {cell("Profit margin", data.profitability?.profit_margin, m?.profit_margin, true,
                      band(data.profitability?.profit_margin, v => v > 0.15, v => v < 0),
                      fmtPct, true)}
                    {cell("D/E", data.financial_health?.debt_to_equity, m?.debt_to_equity, false,
                      band(data.financial_health?.debt_to_equity, v => v >= 0 && v < 50, v => v > 200),
                      fmtNum)}

                    {cell("Rev growth", data.growth?.revenue_growth, m?.revenue_growth, true,
                      band(data.growth?.revenue_growth, v => v > 0.1, v => v < 0),
                      fmtPct, true)}
                    {cell("EPS growth", data.growth?.earnings_growth, m?.earnings_growth, true,
                      band(data.growth?.earnings_growth, v => v > 0.1, v => v < 0),
                      fmtPct, true)}
                    {cell("Div yield", data.dividends?.dividend_yield, m?.dividend_yield, true,
                      "text-zinc-300", fmtPct, true)}
                  </div>
                );
              })()}

              {(data.ownership?.institutional_pct != null || data.ownership?.insider_pct != null) && (
                <div className="text-[10px] text-zinc-500 flex gap-3">
                  {data.ownership?.institutional_pct != null && (
                    <span>Institutions: <span className="text-zinc-300">{fmtPct(data.ownership.institutional_pct)}</span></span>
                  )}
                  {data.ownership?.insider_pct != null && (
                    <span>Insiders: <span className="text-zinc-300">{fmtPct(data.ownership.insider_pct)}</span></span>
                  )}
                  {data.financial_health?.total_cash != null && (
                    <span>Cash: <span className="text-zinc-300">{fmtCr(data.financial_health.total_cash)}</span></span>
                  )}
                </div>
              )}

              <p className="text-[10px] text-zinc-600 italic">
                {data.sector_medians
                  ? `Cells colour-graded vs ${data.sector ?? "sector"} median (deadband ±10%). Medians curated from NIFTY sectoral constituents.`
                  : "Sector median unavailable — colours fall back to absolute bands."}
              </p>
            </>
          )}
        </div>
      )}
    </div>
  );
}
