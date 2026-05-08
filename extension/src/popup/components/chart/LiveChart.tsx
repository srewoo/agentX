import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../../shared/api";
import {
  type Candle,
  type Exchange,
  type Interval,
  buildA11ySummary,
  computeEMA,
  computeMACD,
  computeRSI,
  formatChangePct,
  formatINRPrecise,
  formatVolume,
  intervalToRange,
  toLineData,
} from "./utils";
import { useStreamQuote } from "./useStreamQuote";

export interface LiveChartProps {
  symbol: string;
  exchange: Exchange;
  interval: Interval;
  /** EMA periods to overlay. Default [20, 50, 200]. Pass [] to disable. */
  showEMA?: number[];
  showRSI?: boolean;
  showMACD?: boolean;
  height?: number;
  className?: string;
}

const EMA_COLORS: Record<number, string> = {
  9: "#FBBF24",
  20: "#60A5FA",
  21: "#34D399",
  50: "#F97316",
  200: "#A78BFA",
};

const PERF_WORKER_THRESHOLD = 5000;

interface CrosshairData {
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
  changePct: number | null;
  time: string;
}

/**
 * Live candlestick chart with EMA overlays, optional RSI/MACD subpanes, and
 * a streaming last-tick append. Lightweight-charts is dynamically imported so
 * the chart bundle is only paid for on chart-bearing routes.
 */
export default function LiveChart({
  symbol,
  exchange,
  interval,
  showEMA = [20, 50, 200],
  showRSI = false,
  showMACD = false,
  height = 240,
  className = "",
}: LiveChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<unknown>(null);
  const candleSeriesRef = useRef<unknown>(null);
  const volumeSeriesRef = useRef<unknown>(null);
  const emaSeriesRef = useRef<Map<number, unknown>>(new Map());
  const rsiSeriesRef = useRef<unknown>(null);
  const macdSeriesRef = useRef<unknown>(null);
  const macdSignalRef = useRef<unknown>(null);
  const macdHistRef = useRef<unknown>(null);

  const [candles, setCandles] = useState<Candle[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [hover, setHover] = useState<CrosshairData | null>(null);

  // Live tick — append-to-last-candle.
  const { tick } = useStreamQuote(symbol, exchange, { enabled: !!symbol });

  // ---------- Fetch historical candles ----------
  useEffect(() => {
    if (!symbol) return;
    const ctrl = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError(null);

    const range = intervalToRange(interval);
    api
      .getHistory(symbol, range, interval)
      .then((res) => {
        if (cancelled) return;
        const sorted = [...res.history].sort((a, b) =>
          String(a.date) < String(b.date) ? -1 : 1
        );
        const mapped: Candle[] = sorted.map((d) => ({
          time: d.date,
          open: d.o,
          high: d.h,
          low: d.l,
          close: d.c,
          volume: d.v,
        }));
        setCandles(mapped);
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Failed to load chart");
        setLoading(false);
      });

    return () => {
      cancelled = true;
      ctrl.abort();
    };
  }, [symbol, interval]);

  // ---------- Compute EMA series (worker for large datasets) ----------
  const [emaData, setEmaData] = useState<Record<number, (number | null)[]>>({});
  useEffect(() => {
    if (!candles || candles.length === 0 || showEMA.length === 0) {
      setEmaData({});
      return;
    }
    const closes = candles.map((c) => c.close);

    if (closes.length >= PERF_WORKER_THRESHOLD && typeof Worker !== "undefined") {
      let worker: Worker | null = null;
      let cancelled = false;
      try {
        // Vite-friendly worker construction. Using `new Worker(new URL(...))`
        // gives us a properly bundled chunk.
        worker = new Worker(new URL("./ema.worker.ts", import.meta.url), {
          type: "module",
        });
        const id = Date.now();
        worker.onmessage = (e: MessageEvent) => {
          if (cancelled) return;
          const data = e.data as { id: number; emas: Record<number, (number | null)[]> };
          if (data.id === id) setEmaData(data.emas);
        };
        worker.postMessage({ id, closes, periods: showEMA });
      } catch {
        // Worker construction failed (e.g. sandbox restrictions) — fall back.
        const out: Record<number, (number | null)[]> = {};
        for (const p of showEMA) out[p] = computeEMA(closes, p);
        setEmaData(out);
      }
      return () => {
        cancelled = true;
        worker?.terminate();
      };
    }

    const out: Record<number, (number | null)[]> = {};
    for (const p of showEMA) out[p] = computeEMA(closes, p);
    setEmaData(out);
  }, [candles, showEMA]);

  // ---------- Build chart (dynamic import) ----------
  useEffect(() => {
    if (!containerRef.current || !candles) return;
    const container = containerRef.current;
    let cancelled = false;
    let cleanup: (() => void) | null = null;

    (async () => {
      const lwc = await import("lightweight-charts");
      if (cancelled) return;
      const {
        createChart,
        ColorType,
        CrosshairMode,
        CandlestickSeries,
        HistogramSeries,
        LineSeries,
      } = lwc;

      const chart = createChart(container, {
        height,
        layout: {
          background: { type: ColorType.Solid, color: "#18181B" },
          textColor: "#A1A1AA",
          fontSize: 10,
        },
        grid: {
          vertLines: { color: "#27272A" },
          horzLines: { color: "#27272A" },
        },
        crosshair: { mode: CrosshairMode.Normal },
        rightPriceScale: {
          borderColor: "#27272A",
          scaleMargins: { top: 0.05, bottom: showRSI || showMACD ? 0.35 : 0.2 },
        },
        timeScale: {
          borderColor: "#27272A",
          timeVisible: ["1m", "5m", "15m", "1h"].includes(interval),
        },
      });
      chartRef.current = chart;

      const candleSeries = chart.addSeries(CandlestickSeries, {
        upColor: "#10B981",
        downColor: "#EF4444",
        borderUpColor: "#10B981",
        borderDownColor: "#EF4444",
        wickUpColor: "#10B981",
        wickDownColor: "#EF4444",
      });
      candleSeriesRef.current = candleSeries;

      const volumeSeries = chart.addSeries(HistogramSeries, {
        priceFormat: { type: "volume" },
        priceScaleId: "volume",
      });
      volumeSeries.priceScale().applyOptions({
        scaleMargins: { top: 0.85, bottom: 0 },
      });
      volumeSeriesRef.current = volumeSeries;

      // EMA series
      emaSeriesRef.current = new Map();
      for (const period of showEMA) {
        const s = chart.addSeries(LineSeries, {
          color: EMA_COLORS[period] ?? "#A1A1AA",
          lineWidth: 1,
          priceLineVisible: false,
          lastValueVisible: false,
        });
        emaSeriesRef.current.set(period, s);
      }

      // RSI subpane (own price scale)
      if (showRSI) {
        const rsiSeries = chart.addSeries(LineSeries, {
          color: "#A78BFA",
          lineWidth: 1,
          priceLineVisible: false,
          lastValueVisible: false,
          priceScaleId: "rsi",
        });
        rsiSeries.priceScale().applyOptions({
          scaleMargins: { top: 0.7, bottom: showMACD ? 0.15 : 0 },
        });
        rsiSeriesRef.current = rsiSeries;
      }
      if (showMACD) {
        const macdSeries = chart.addSeries(LineSeries, {
          color: "#60A5FA",
          lineWidth: 1,
          priceLineVisible: false,
          lastValueVisible: false,
          priceScaleId: "macd",
        });
        const sigSeries = chart.addSeries(LineSeries, {
          color: "#FBBF24",
          lineWidth: 1,
          priceLineVisible: false,
          lastValueVisible: false,
          priceScaleId: "macd",
        });
        const histSeries = chart.addSeries(HistogramSeries, {
          priceLineVisible: false,
          priceScaleId: "macd",
        });
        macdSeries.priceScale().applyOptions({
          scaleMargins: { top: showRSI ? 0.85 : 0.75, bottom: 0 },
        });
        macdSeriesRef.current = macdSeries;
        macdSignalRef.current = sigSeries;
        macdHistRef.current = histSeries;
      }

      // Crosshair → tooltip data
      chart.subscribeCrosshairMove((param: unknown) => {
        const p = param as {
          time?: string | number;
          seriesData?: Map<unknown, { open: number; high: number; low: number; close: number }>;
        };
        if (!p.time || !p.seriesData) {
          setHover(null);
          return;
        }
        const cd = p.seriesData.get(candleSeries) as
          | { open: number; high: number; low: number; close: number }
          | undefined;
        if (!cd) {
          setHover(null);
          return;
        }
        // Find matching candle for volume + change%
        const target = String(p.time);
        const match = candles.find((c) => String(c.time) === target);
        const prev = match
          ? candles[candles.indexOf(match) - 1]
          : undefined;
        const changePct =
          prev && prev.close !== 0
            ? ((cd.close - prev.close) / prev.close) * 100
            : null;
        setHover({
          o: cd.open,
          h: cd.high,
          l: cd.low,
          c: cd.close,
          v: match?.volume ?? 0,
          changePct,
          time: target,
        });
      });

      // ResizeObserver
      const ro = new ResizeObserver((entries) => {
        for (const entry of entries) {
          const w = entry.contentRect.width;
          if (w > 0) chart.applyOptions({ width: w });
        }
      });
      ro.observe(container);

      cleanup = () => {
        ro.disconnect();
        chart.remove();
        chartRef.current = null;
        candleSeriesRef.current = null;
        volumeSeriesRef.current = null;
        emaSeriesRef.current.clear();
        rsiSeriesRef.current = null;
        macdSeriesRef.current = null;
        macdSignalRef.current = null;
        macdHistRef.current = null;
      };
    })();

    return () => {
      cancelled = true;
      cleanup?.();
    };
    // We intentionally only rebuild on the inputs that affect chart structure,
    // not on `candles` (data is pushed to the existing series in the next effect).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [height, interval, showEMA.join(","), showRSI, showMACD, candles != null]);

  // ---------- Push data into series ----------
  useEffect(() => {
    if (!candles || !candleSeriesRef.current) return;
    const candleSeries = candleSeriesRef.current as {
      setData: (d: unknown[]) => void;
    };
    const volumeSeries = volumeSeriesRef.current as {
      setData: (d: unknown[]) => void;
    } | null;

    candleSeries.setData(
      candles.map((c) => ({
        time: c.time,
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
      }))
    );
    volumeSeries?.setData(
      candles.map((c) => ({
        time: c.time,
        value: c.volume,
        color: c.close >= c.open ? "rgba(16,185,129,0.4)" : "rgba(239,68,68,0.4)",
      }))
    );

    const times = candles.map((c) => c.time);
    for (const [period, series] of emaSeriesRef.current) {
      const data = emaData[period];
      if (!data) continue;
      (series as { setData: (d: unknown[]) => void }).setData(
        toLineData(times, data)
      );
    }

    if (rsiSeriesRef.current) {
      const rsi = computeRSI(candles.map((c) => c.close));
      (rsiSeriesRef.current as { setData: (d: unknown[]) => void }).setData(
        toLineData(times, rsi)
      );
    }
    if (macdSeriesRef.current && macdSignalRef.current && macdHistRef.current) {
      const { macd, signal, hist } = computeMACD(candles.map((c) => c.close));
      (macdSeriesRef.current as { setData: (d: unknown[]) => void }).setData(
        toLineData(times, macd)
      );
      (macdSignalRef.current as { setData: (d: unknown[]) => void }).setData(
        toLineData(times, signal)
      );
      const histData = times
        .map((t, i) => {
          const h = hist[i];
          if (h == null) return null;
          return {
            time: t,
            value: h,
            color: h >= 0 ? "rgba(16,185,129,0.5)" : "rgba(239,68,68,0.5)",
          };
        })
        .filter((d): d is NonNullable<typeof d> => d != null);
      (macdHistRef.current as { setData: (d: unknown[]) => void }).setData(histData);
    }
  }, [candles, emaData]);

  // ---------- Append live tick to last candle ----------
  useEffect(() => {
    if (!tick || !candleSeriesRef.current || !candles || candles.length === 0)
      return;
    const last = candles[candles.length - 1];
    const updated = {
      time: last.time,
      open: last.open,
      high: Math.max(last.high, tick.price),
      low: Math.min(last.low, tick.price),
      close: tick.price,
    };
    (candleSeriesRef.current as { update: (d: unknown) => void }).update(updated);
  }, [tick, candles]);

  // ---------- A11y summary ----------
  const a11ySummary = useMemo(() => {
    if (!candles || candles.length === 0) return `${symbol} chart loading.`;
    const last = tick?.price ?? candles[candles.length - 1].close;
    const prevClose =
      candles.length >= 2 ? candles[candles.length - 2].close : null;
    const changePct =
      tick?.changePct ??
      (prevClose ? ((last - prevClose) / prevClose) * 100 : null);
    const recent = candles.slice(-5);
    const rangeLow = Math.min(...recent.map((c) => c.low));
    const rangeHigh = Math.max(...recent.map((c) => c.high));
    return buildA11ySummary({
      symbol,
      changePct,
      lastPrice: last,
      rangeLow,
      rangeHigh,
      rangeDays: recent.length,
    });
  }, [symbol, candles, tick]);

  // ---------- Render ----------
  return (
    <section
      aria-label={`Live chart for ${symbol}`}
      className={`relative w-full rounded-lg overflow-hidden bg-zinc-900/40 ${className}`}
    >
      {/* Screen-reader-only summary */}
      <p className="sr-only" aria-live="polite">
        {a11ySummary}
      </p>

      {/* Hover tooltip */}
      {hover && (
        <div
          role="status"
          aria-live="polite"
          className="absolute top-1 left-1 z-10 text-[10px] font-mono bg-zinc-950/90 border border-zinc-700 rounded px-2 py-1 text-zinc-200 pointer-events-none"
        >
          <span className="text-zinc-500">O</span> {formatINRPrecise(hover.o)}{" "}
          <span className="text-zinc-500">H</span> {formatINRPrecise(hover.h)}{" "}
          <span className="text-zinc-500">L</span> {formatINRPrecise(hover.l)}{" "}
          <span className="text-zinc-500">C</span> {formatINRPrecise(hover.c)}{" "}
          <span className="text-zinc-500">V</span> {formatVolume(hover.v)}{" "}
          <span
            className={
              hover.changePct == null
                ? "text-zinc-500"
                : hover.changePct >= 0
                ? "text-emerald-400"
                : "text-rose-400"
            }
          >
            {formatChangePct(hover.changePct)}
          </span>
        </div>
      )}

      {loading && (
        <div
          className="absolute inset-0 z-20 flex items-center justify-center bg-zinc-900/80"
          style={{ height }}
          role="status"
          aria-live="polite"
        >
          <div className="flex flex-col items-center gap-2">
            <div className="w-6 h-6 border-2 border-zinc-600 border-t-zinc-300 rounded-full animate-spin" />
            <span className="text-xs text-zinc-400">Loading chart…</span>
          </div>
        </div>
      )}

      {error && !loading && (
        <div
          role="alert"
          className="flex flex-col items-center justify-center gap-1 text-xs text-zinc-400"
          style={{ height }}
        >
          <span>Chart unavailable</span>
          <span className="text-[10px] text-zinc-500">{error}</span>
        </div>
      )}

      {!error && candles && candles.length === 0 && !loading && (
        <div
          className="flex items-center justify-center text-xs text-zinc-500"
          style={{ height }}
        >
          No data for this timeframe
        </div>
      )}

      <div ref={containerRef} style={{ height }} />
    </section>
  );
}
