import { useMemo } from "react";
import type { Candle } from "./utils";
import { formatVolume } from "./utils";

interface VolumeBarsProps {
  candles: Candle[];
  /** Number of trailing bars to render. Defaults to 30. */
  count?: number;
  width?: number;
  height?: number;
  className?: string;
}

/**
 * Compact standalone volume strip — used in cards/tooltips where the main
 * `LiveChart` would be overkill. The `LiveChart` component renders its own
 * volume histogram via lightweight-charts.
 *
 * Bars are colored by candle direction (green up, red down) to match the
 * candlestick palette.
 */
export default function VolumeBars({
  candles,
  count = 30,
  width = 120,
  height = 32,
  className = "",
}: VolumeBarsProps) {
  const slice = useMemo(
    () => candles.slice(-count),
    [candles, count]
  );

  const { bars, ariaLabel } = useMemo(() => {
    if (slice.length === 0) {
      return { bars: [], ariaLabel: "No volume data" };
    }
    const maxV = slice.reduce((m, c) => Math.max(m, c.volume || 0), 0) || 1;
    const gap = 1;
    const barW = Math.max(1, (width - gap * (slice.length - 1)) / slice.length);
    const out = slice.map((c, i) => {
      const h = ((c.volume || 0) / maxV) * height;
      return {
        x: i * (barW + gap),
        y: height - h,
        w: barW,
        h,
        up: c.close >= c.open,
      };
    });
    const total = slice.reduce((s, c) => s + (c.volume || 0), 0);
    return {
      bars: out,
      ariaLabel: `${slice.length}-bar volume, total ${formatVolume(total)}`,
    };
  }, [slice, width, height]);

  if (bars.length === 0) {
    return (
      <div
        role="img"
        aria-label={ariaLabel}
        className={`flex items-center justify-center text-[10px] text-zinc-600 ${className}`}
        style={{ width, height }}
      >
        —
      </div>
    );
  }

  return (
    <svg
      role="img"
      aria-label={ariaLabel}
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className={className}
    >
      {bars.map((b, i) => (
        <rect
          key={i}
          x={b.x}
          y={b.y}
          width={b.w}
          height={b.h}
          fill={b.up ? "rgba(16,185,129,0.7)" : "rgba(239,68,68,0.7)"}
        />
      ))}
    </svg>
  );
}
