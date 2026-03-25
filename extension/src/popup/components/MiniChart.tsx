import { useEffect, useRef, useState, useCallback } from "react";
import { createChart, type IChartApi, type ISeriesApi, ColorType, CrosshairMode, LineStyle, CandlestickSeries, HistogramSeries, LineSeries } from "lightweight-charts";
import { api } from "../../shared/api";

interface Props {
  symbol: string;
  height?: number;
}

interface Timeframe {
  label: string;
  period: string;
  interval: string;
}

const TIMEFRAMES: Timeframe[] = [
  { label: "1D", period: "1d", interval: "5m" },
  { label: "1W", period: "5d", interval: "15m" },
  { label: "1M", period: "1mo", interval: "1d" },
  { label: "3M", period: "3mo", interval: "1d" },
];

/** Compute SMA for an array of closes at a given window size. Returns array aligned to input (NaN for insufficient data). */
function computeSMA(closes: number[], window: number): (number | null)[] {
  const result: (number | null)[] = [];
  for (let i = 0; i < closes.length; i++) {
    if (i < window - 1) {
      result.push(null);
    } else {
      let sum = 0;
      for (let j = i - window + 1; j <= i; j++) {
        sum += closes[j];
      }
      result.push(sum / window);
    }
  }
  return result;
}

/** Compute Bollinger Bands (SMA20 +/- 2*stddev). Returns { upper, lower } arrays. */
function computeBollingerBands(closes: number[], window = 20, mult = 2): { upper: (number | null)[]; lower: (number | null)[] } {
  const upper: (number | null)[] = [];
  const lower: (number | null)[] = [];
  for (let i = 0; i < closes.length; i++) {
    if (i < window - 1) {
      upper.push(null);
      lower.push(null);
    } else {
      let sum = 0;
      for (let j = i - window + 1; j <= i; j++) {
        sum += closes[j];
      }
      const mean = sum / window;
      let sqSum = 0;
      for (let j = i - window + 1; j <= i; j++) {
        sqSum += (closes[j] - mean) ** 2;
      }
      const stddev = Math.sqrt(sqSum / window);
      upper.push(mean + mult * stddev);
      lower.push(mean - mult * stddev);
    }
  }
  return { upper, lower };
}

export default function MiniChart({ symbol, height = 150 }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const sma20SeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const sma50SeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const bbUpperSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const bbLowerSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [showSMA, setShowSMA] = useState(false);
  const [showBB, setShowBB] = useState(false);
  const [activeTimeframe, setActiveTimeframe] = useState(3); // index into TIMEFRAMES, default "3M"

  // Toggle overlay visibility
  useEffect(() => {
    if (sma20SeriesRef.current) {
      sma20SeriesRef.current.applyOptions({ visible: showSMA });
    }
    if (sma50SeriesRef.current) {
      sma50SeriesRef.current.applyOptions({ visible: showSMA });
    }
  }, [showSMA]);

  useEffect(() => {
    if (bbUpperSeriesRef.current) {
      bbUpperSeriesRef.current.applyOptions({ visible: showBB });
    }
    if (bbLowerSeriesRef.current) {
      bbLowerSeriesRef.current.applyOptions({ visible: showBB });
    }
  }, [showBB]);

  // Create chart and fetch data
  useEffect(() => {
    if (!containerRef.current) return;

    const container = containerRef.current;
    const tf = TIMEFRAMES[activeTimeframe];
    const isIntraday = ["5m", "15m", "30m", "1h"].includes(tf.interval);

    const chart = createChart(container, {
      height,
      layout: {
        background: { type: ColorType.Solid, color: "#18181B" },
        textColor: "#71717A",
        fontSize: 10,
      },
      grid: {
        vertLines: { color: "#27272A" },
        horzLines: { color: "#27272A" },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: "#52525B", width: 1, style: 3, labelBackgroundColor: "#27272A" },
        horzLine: { color: "#52525B", width: 1, style: 3, labelBackgroundColor: "#27272A" },
      },
      rightPriceScale: {
        borderColor: "#27272A",
        scaleMargins: { top: 0.05, bottom: 0.2 },
      },
      timeScale: {
        borderColor: "#27272A",
        timeVisible: isIntraday,
        fixLeftEdge: true,
        fixRightEdge: true,
      },
      handleScroll: false,
      handleScale: false,
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
      priceScaleId: "",
    });
    volumeSeries.priceScale().applyOptions({
      scaleMargins: { top: 0.85, bottom: 0 },
    });
    volumeSeriesRef.current = volumeSeries;

    // Overlay series
    const sma20Series = chart.addSeries(LineSeries, {
      color: "#60A5FA",
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      visible: showSMA,
    });
    sma20SeriesRef.current = sma20Series;

    const sma50Series = chart.addSeries(LineSeries, {
      color: "#F97316",
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      visible: showSMA,
    });
    sma50SeriesRef.current = sma50Series;

    const bbUpperSeries = chart.addSeries(LineSeries, {
      color: "#A78BFA",
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      priceLineVisible: false,
      lastValueVisible: false,
      visible: showBB,
    });
    bbUpperSeriesRef.current = bbUpperSeries;

    const bbLowerSeries = chart.addSeries(LineSeries, {
      color: "#A78BFA",
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      priceLineVisible: false,
      lastValueVisible: false,
      visible: showBB,
    });
    bbLowerSeriesRef.current = bbLowerSeries;

    // Fetch data
    let cancelled = false;

    api.getHistory(symbol, tf.period, tf.interval)
      .then((res) => {
        if (cancelled) return;

        const sorted = [...res.history].sort((a, b) => {
          if (typeof a.date === "number" && typeof b.date === "number") return a.date - b.date;
          return String(a.date) < String(b.date) ? -1 : 1;
        });

        const candles = sorted.map((d) => ({
          time: d.date as string,
          open: d.o,
          high: d.h,
          low: d.l,
          close: d.c,
        }));

        const volumes = sorted.map((d) => ({
          time: d.date as string,
          value: d.v,
          color: d.c >= d.o ? "rgba(16,185,129,0.3)" : "rgba(239,68,68,0.3)",
        }));

        candleSeries.setData(candles);
        volumeSeries.setData(volumes);

        // Compute and set overlay data
        const closes = sorted.map((d) => d.c);
        const times = sorted.map((d) => d.date);

        const sma20Data = computeSMA(closes, 20);
        const sma50Data = computeSMA(closes, 50);
        const bb = computeBollingerBands(closes, 20, 2);

        const toLineData = (values: (number | null)[]) =>
          values
            .map((v, i) => (v !== null ? { time: times[i] as string, value: v } : null))
            .filter((d): d is { time: string; value: number } => d !== null);

        sma20Series.setData(toLineData(sma20Data));
        sma50Series.setData(toLineData(sma50Data));
        bbUpperSeries.setData(toLineData(bb.upper));
        bbLowerSeries.setData(toLineData(bb.lower));

        chart.timeScale().fitContent();
        setLoading(false);
      })
      .catch(() => {
        if (!cancelled) {
          setError(true);
          setLoading(false);
        }
      });

    // Auto-resize
    const resizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width } = entry.contentRect;
        if (width > 0) {
          chart.applyOptions({ width });
        }
      }
    });
    resizeObserver.observe(container);

    return () => {
      cancelled = true;
      resizeObserver.disconnect();
      chart.remove();
      chartRef.current = null;
      candleSeriesRef.current = null;
      volumeSeriesRef.current = null;
      sma20SeriesRef.current = null;
      sma50SeriesRef.current = null;
      bbUpperSeriesRef.current = null;
      bbLowerSeriesRef.current = null;
    };
  }, [symbol, height, activeTimeframe]);

  if (error) {
    return (
      <div
        className="flex items-center justify-center text-xs text-zinc-500 bg-zinc-900/40 rounded-lg"
        style={{ height }}
      >
        Chart unavailable
      </div>
    );
  }

  return (
    <div className="relative w-full rounded-lg overflow-hidden">
      {/* Overlay toggle buttons */}
      <div className="flex items-center gap-1 px-2 py-1 bg-zinc-900/60">
        <button
          onClick={() => setShowSMA((v) => !v)}
          className={`text-[10px] px-1.5 py-0.5 rounded border font-medium transition-colors ${
            showSMA
              ? "border-blue-500/50 text-blue-400 bg-blue-500/10"
              : "border-zinc-700 text-zinc-500 hover:text-zinc-400"
          }`}
        >
          SMA
        </button>
        <button
          onClick={() => setShowBB((v) => !v)}
          className={`text-[10px] px-1.5 py-0.5 rounded border font-medium transition-colors ${
            showBB
              ? "border-purple-500/50 text-purple-400 bg-purple-500/10"
              : "border-zinc-700 text-zinc-500 hover:text-zinc-400"
          }`}
        >
          BB
        </button>
        {showSMA && (
          <span className="text-[9px] text-zinc-600 ml-1">
            <span className="text-blue-400">SMA20</span>{" "}
            <span className="text-orange-400">SMA50</span>
          </span>
        )}
      </div>

      {loading && (
        <div
          className="absolute inset-0 z-10 bg-zinc-900/80 rounded-lg animate-pulse flex items-center justify-center"
          style={{ height }}
        >
          <div className="flex flex-col items-center gap-2">
            <div className="w-8 h-8 border-2 border-zinc-600 border-t-zinc-400 rounded-full animate-spin" />
            <span className="text-xs text-zinc-500">Loading chart...</span>
          </div>
        </div>
      )}
      <div ref={containerRef} style={{ height }} />

      {/* Timeframe toggle buttons */}
      <div className="flex items-center justify-center gap-1 px-2 py-1 bg-zinc-900/60">
        {TIMEFRAMES.map((tf, idx) => (
          <button
            key={tf.label}
            onClick={() => {
              if (idx !== activeTimeframe) {
                setLoading(true);
                setError(false);
                setActiveTimeframe(idx);
              }
            }}
            className={`text-[10px] px-2 py-0.5 rounded font-medium transition-colors ${
              idx === activeTimeframe
                ? "bg-zinc-700 text-zinc-200"
                : "text-zinc-500 hover:text-zinc-300"
            }`}
          >
            {tf.label}
          </button>
        ))}
      </div>
    </div>
  );
}
