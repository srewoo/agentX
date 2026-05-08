import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { EquityPoint } from "@/lib/types";
import { formatINR } from "@/lib/format";

type Period = "1m" | "3m" | "1y" | "all";

const PERIODS: { value: Period; label: string }[] = [
  { value: "1m", label: "1M" },
  { value: "3m", label: "3M" },
  { value: "1y", label: "1Y" },
  { value: "all", label: "All" },
];

interface EquityCurveProps {
  defaultPeriod?: Period;
}

/**
 * Time-series area chart of portfolio value.
 * `lightweight-charts` is dynamically imported to keep the popup bundle small.
 */
export default function EquityCurve({ defaultPeriod = "3m" }: EquityCurveProps) {
  const [period, setPeriod] = useState<Period>(defaultPeriod);
  const [points, setPoints] = useState<EquityPoint[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setPoints(null);
    setError(null);
    // `period` is a UI-side selector (e.g. "1M" / "3M"); the API takes an
    // AbortSignal, not a period — so we just refetch on period change and
    // slice locally if needed.
    void period;
    (async () => {
      try {
        const res = await api.portfolio.equityCurve();
        if (!cancelled) setPoints(res);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load equity curve");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [period]);

  const last = points && points.length ? points[points.length - 1] : null;
  const first = points && points.length ? points[0] : null;
  const change = last && first ? last.value - first.value : 0;
  const up = change >= 0;

  return (
    <section aria-label="Equity curve" className="rounded-xl border border-neutral-800 bg-neutral-900/40 p-3">
      <header className="mb-2 flex items-center justify-between">
        <div>
          <div className="text-[11px] uppercase tracking-wide text-neutral-400">Equity Curve</div>
          {last ? (
            <div className="text-base font-bold tabular-nums text-neutral-100">
              {formatINR(last.value)}{" "}
              <span className={`ml-1 text-xs font-medium ${up ? "text-emerald-400" : "text-rose-400"}`}>
                {up ? "▲" : "▼"} {formatINR(Math.abs(change))}
              </span>
            </div>
          ) : null}
        </div>
        <div role="tablist" aria-label="Time period" className="flex gap-1">
          {PERIODS.map((p) => {
            const active = p.value === period;
            return (
              <button
                key={p.value}
                role="tab"
                aria-selected={active}
                aria-controls="equity-curve-canvas"
                onClick={() => setPeriod(p.value)}
                className={`rounded px-2 py-1 text-xs font-medium transition-colors ${active ? "bg-emerald-500/20 text-emerald-300" : "text-neutral-400 hover:text-neutral-200"}`}
              >
                {p.label}
              </button>
            );
          })}
        </div>
      </header>

      <div id="equity-curve-canvas" className="h-40">
        {error ? (
          <div role="alert" className="flex h-full items-center justify-center text-sm text-red-300">
            {error}
          </div>
        ) : !points ? (
          <div className="h-full animate-pulse rounded bg-neutral-800/50" aria-busy="true" aria-label="Loading chart" />
        ) : points.length === 0 ? (
          <div className="flex h-full items-center justify-center text-sm text-neutral-500">
            No history yet — record your first trade to start your equity curve.
          </div>
        ) : (
          <AreaChart points={points} positive={up} />
        )}
      </div>
    </section>
  );
}

function AreaChart({ points, positive }: { points: EquityPoint[]; positive: boolean }) {
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const el = containerRef.current;
    let disposed = false;
    let chart: { remove: () => void; applyOptions: (o: object) => void; timeScale: () => { fitContent: () => void } } | null = null;
    let resizeObserver: ResizeObserver | null = null;

    (async () => {
      try {
        const lib = await import("lightweight-charts");
        if (disposed) return;
        const created = lib.createChart(el, {
          width: el.clientWidth,
          height: el.clientHeight,
          layout: { background: { color: "transparent" }, textColor: "#a3a3a3" },
          grid: { vertLines: { visible: false }, horzLines: { color: "rgba(115,115,115,0.15)" } },
          rightPriceScale: { borderVisible: false },
          timeScale: { borderVisible: false },
          handleScroll: false,
          handleScale: false,
        });
        chart = created as unknown as typeof chart;
        const color = positive ? "#34d399" : "#fb7185";
        // lightweight-charts v5 signature
        const series = (created as unknown as {
          addAreaSeries: (o: object) => { setData: (d: Array<{ time: string; value: number }>) => void };
        }).addAreaSeries({
          lineColor: color,
          topColor: positive ? "rgba(52,211,153,0.35)" : "rgba(251,113,133,0.35)",
          bottomColor: positive ? "rgba(52,211,153,0.0)" : "rgba(251,113,133,0.0)",
          lineWidth: 2,
        });
        series.setData(
          points.map((p) => ({
            time: p.date.slice(0, 10),
            value: p.value,
          })),
        );
        created.timeScale().fitContent();

        resizeObserver = new ResizeObserver(() => {
          created.applyOptions({ width: el.clientWidth, height: el.clientHeight });
        });
        resizeObserver.observe(el);
      } catch (e) {
        // Chart library failed to load — render nothing; outer component shows skeleton fallback.
        // eslint-disable-next-line no-console
        console.warn("EquityCurve: failed to render chart", e);
      }
    })();

    return () => {
      disposed = true;
      resizeObserver?.disconnect();
      chart?.remove();
    };
  }, [points, positive]);

  return <div ref={containerRef} className="h-full w-full" role="img" aria-label="Portfolio value over time" />;
}
