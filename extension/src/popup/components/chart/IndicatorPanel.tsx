import { useMemo } from "react";
import { computeRSI, computeMACD } from "./utils";

interface IndicatorPanelProps {
  closes: number[];
  kind: "rsi" | "macd";
  width?: number;
  height?: number;
  className?: string;
}

/**
 * Standalone RSI / MACD subpane. Renders a compact SVG visualization aligned
 * to a closes series. Used when an indicator is requested but a full
 * lightweight-charts panel would be too heavy (e.g. inside a tooltip card,
 * or under a `MiniSparkline`).
 *
 * `LiveChart` integrates these indicators directly into its main chart via
 * separate price scales — use this component for *secondary* surfaces.
 */
export default function IndicatorPanel({
  closes,
  kind,
  width = 200,
  height = 48,
  className = "",
}: IndicatorPanelProps) {
  const series = useMemo(() => {
    if (kind === "rsi") {
      const rsi = computeRSI(closes, 14);
      return { kind: "rsi" as const, rsi };
    }
    const { macd, signal, hist } = computeMACD(closes);
    return { kind: "macd" as const, macd, signal, hist };
  }, [closes, kind]);

  if (closes.length < 30) {
    return (
      <div
        role="img"
        aria-label={`${kind.toUpperCase()} unavailable — not enough data`}
        className={`text-[10px] text-zinc-600 italic flex items-center justify-center ${className}`}
        style={{ width, height }}
      >
        Not enough data
      </div>
    );
  }

  if (series.kind === "rsi") {
    const valid = series.rsi
      .map((v, i) => ({ v, i }))
      .filter((p): p is { v: number; i: number } => p.v != null);
    const stepX = width / Math.max(1, closes.length - 1);
    const path =
      "M " +
      valid
        .map((p) => {
          const x = p.i * stepX;
          const y = height - (p.v / 100) * height;
          return `${x.toFixed(1)},${y.toFixed(1)}`;
        })
        .join(" L ");
    const last = valid[valid.length - 1]?.v ?? null;
    const overbought = height - (70 / 100) * height;
    const oversold = height - (30 / 100) * height;
    return (
      <svg
        role="img"
        aria-label={`RSI 14, latest ${last == null ? "unknown" : last.toFixed(1)}`}
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        className={className}
      >
        <line
          x1={0}
          x2={width}
          y1={overbought}
          y2={overbought}
          stroke="#52525B"
          strokeDasharray="2 2"
        />
        <line
          x1={0}
          x2={width}
          y1={oversold}
          y2={oversold}
          stroke="#52525B"
          strokeDasharray="2 2"
        />
        <path
          d={path}
          fill="none"
          stroke="#A78BFA"
          strokeWidth={1.25}
          strokeLinejoin="round"
        />
      </svg>
    );
  }

  // MACD
  const { macd, signal, hist } = series;
  const allVals = [...macd, ...signal].filter(
    (v): v is number => v != null && Number.isFinite(v)
  );
  if (allVals.length === 0) return null;
  const min = Math.min(...allVals);
  const max = Math.max(...allVals);
  const range = max - min || 1;
  const stepX = width / Math.max(1, closes.length - 1);
  const yOf = (v: number) => height - ((v - min) / range) * height;
  const zeroY = yOf(0);

  const macdPath =
    "M " +
    macd
      .map((v, i) => (v != null ? `${(i * stepX).toFixed(1)},${yOf(v).toFixed(1)}` : ""))
      .filter(Boolean)
      .join(" L ");
  const sigPath =
    "M " +
    signal
      .map((v, i) => (v != null ? `${(i * stepX).toFixed(1)},${yOf(v).toFixed(1)}` : ""))
      .filter(Boolean)
      .join(" L ");

  const lastMacd = [...macd].reverse().find((v): v is number => v != null) ?? null;

  return (
    <svg
      role="img"
      aria-label={`MACD 12 26 9, latest ${
        lastMacd == null ? "unknown" : lastMacd.toFixed(2)
      }`}
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className={className}
    >
      {hist.map((h, i) => {
        if (h == null) return null;
        const x = i * stepX;
        const y = yOf(h);
        const top = Math.min(y, zeroY);
        const barH = Math.abs(y - zeroY);
        return (
          <rect
            key={i}
            x={x - stepX / 3}
            y={top}
            width={Math.max(1, stepX * 0.6)}
            height={Math.max(0.5, barH)}
            fill={h >= 0 ? "rgba(16,185,129,0.5)" : "rgba(239,68,68,0.5)"}
          />
        );
      })}
      <line x1={0} x2={width} y1={zeroY} y2={zeroY} stroke="#52525B" />
      <path d={macdPath} fill="none" stroke="#60A5FA" strokeWidth={1.25} />
      <path d={sigPath} fill="none" stroke="#FBBF24" strokeWidth={1.25} />
    </svg>
  );
}
