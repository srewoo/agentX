import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { PortfolioSummaryData } from "@/lib/types";
import { formatINR, formatINRPrecise, formatPct } from "@/lib/format";
import MiniSparkline from "@/components/chart/MiniSparkline";

interface PortfolioSummaryProps {
  /** Optional sparkline series for total P&L trend. */
  pnlSeries?: number[];
  /** Optional override; if not provided, fetches from `api.portfolio.summary()`. */
  data?: PortfolioSummaryData;
}

type Status = "loading" | "ready" | "error";

/**
 * Hero KPI strip: total value, day P&L, total P&L, Sharpe, drawdown, beta.
 * Big bold numbers with Indian formatting and directional color.
 */
export default function PortfolioSummary({ pnlSeries, data: overrideData }: PortfolioSummaryProps) {
  const [data, setData] = useState<PortfolioSummaryData | null>(overrideData ?? null);
  const [status, setStatus] = useState<Status>(overrideData ? "ready" : "loading");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (overrideData) {
      setData(overrideData);
      setStatus("ready");
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        setStatus("loading");
        const res = await api.portfolio.summary();
        if (cancelled) return;
        setData(res);
        setStatus("ready");
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Failed to load portfolio summary");
        setStatus("error");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [overrideData]);

  if (status === "loading") {
    return (
      <section
        aria-label="Portfolio summary"
        aria-busy="true"
        className="grid grid-cols-2 gap-3 rounded-xl bg-neutral-900/40 p-4 sm:grid-cols-3 lg:grid-cols-6"
      >
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="space-y-2">
            <div className="h-3 w-16 animate-pulse rounded bg-neutral-700/60" />
            <div className="h-7 w-24 animate-pulse rounded bg-neutral-700/60" />
          </div>
        ))}
      </section>
    );
  }

  if (status === "error" || !data) {
    return (
      <section
        aria-label="Portfolio summary"
        role="alert"
        className="rounded-xl border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-300"
      >
        {error ?? "Unable to load portfolio summary."}
      </section>
    );
  }

  // Tolerate optional analytics fields — backend may not yet ship sharpe/
  // beta/maxDrawdown. Show "—" instead of NaN.
  const totalPnlV = data.totalPnl ?? data.pnl ?? 0;
  const totalPnlPctV = data.totalPnlPct ?? data.pnlPct ?? 0;
  const totalValueV = data.totalValue ?? data.marketValue ?? 0;
  const capitalV = data.capital ?? data.invested ?? 0;
  const dayUp = data.dayPnl >= 0;
  const totalUp = totalPnlV >= 0;
  const fmtNum = (v: number | null | undefined, digits = 2) =>
    v == null || Number.isNaN(v) ? "—" : v.toFixed(digits);

  return (
    <section
      aria-label="Portfolio summary"
      className="grid grid-cols-2 gap-3 rounded-xl bg-neutral-900/40 p-4 sm:grid-cols-3 lg:grid-cols-6"
    >
      <Kpi label="Total Value" value={formatINR(totalValueV)} sub={`Capital ${formatINR(capitalV)}`} />

      <Kpi
        label="Day P&L"
        value={`${dayUp ? "↑" : "↓"} ${formatINR(Math.abs(data.dayPnl))}`}
        sub={formatPct(data.dayPnlPct)}
        tone={dayUp ? "up" : "down"}
        ariaValue={`${dayUp ? "up" : "down"} ${formatINRPrecise(Math.abs(data.dayPnl))}`}
      />

      <Kpi
        label="Total P&L"
        value={`${totalUp ? "+" : "-"}${formatINR(Math.abs(totalPnlV))}`}
        sub={formatPct(totalPnlPctV)}
        tone={totalUp ? "up" : "down"}
        accessory={pnlSeries && pnlSeries.length > 1 ? <MiniSparkline values={pnlSeries} width={64} height={20} /> : undefined}
      />

      <Kpi label="Sharpe" value={fmtNum(data.sharpe)} sub="Risk-adjusted" />
      <Kpi
        label="Max Drawdown"
        value={data.maxDrawdown != null ? formatPct(data.maxDrawdown) : "—"}
        sub={data.maxDrawdown != null && data.maxDrawdown < -0.2 ? "High" : "Within range"}
        tone="down"
      />
      <Kpi
        label="Beta"
        value={fmtNum(data.beta)}
        sub={
          data.beta == null
            ? "—"
            : Math.abs(data.beta - 1) < 0.15
              ? "≈ market"
              : data.beta > 1
                ? "Aggressive"
                : "Defensive"
        }
      />
    </section>
  );
}

interface KpiProps {
  label: string;
  value: string;
  sub?: string;
  tone?: "up" | "down" | "neutral";
  accessory?: React.ReactNode;
  ariaValue?: string;
}

function Kpi({ label, value, sub, tone = "neutral", accessory, ariaValue }: KpiProps) {
  const toneClass =
    tone === "up" ? "text-emerald-400" : tone === "down" ? "text-rose-400" : "text-neutral-100";
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wide text-neutral-400">{label}</div>
      <div className="flex items-baseline gap-2">
        <div
          className={`text-xl font-bold tabular-nums ${toneClass}`}
          aria-label={ariaValue}
        >
          {value}
        </div>
        {accessory}
      </div>
      {sub ? <div className="mt-0.5 text-xs text-neutral-500">{sub}</div> : null}
    </div>
  );
}
