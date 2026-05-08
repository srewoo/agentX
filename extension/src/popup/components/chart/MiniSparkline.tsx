import { useMemo } from "react";

interface MiniSparklineProps {
  /** Series of close prices, oldest → newest. */
  values: number[];
  /** Pixel width. Defaults to 80. */
  width?: number;
  /** Pixel height. Defaults to 24. */
  height?: number;
  /** Stroke color override; defaults to direction-based green/red. */
  stroke?: string;
  /** Tooltip / a11y label. */
  label?: string;
  className?: string;
}

/**
 * Tiny inline sparkline. Pure SVG — no chart-lib bundle cost, safe to use
 * inside cards, list rows, and tab headers without measurable perf impact.
 *
 * Direction (last vs first) drives color when no `stroke` is provided.
 */
export default function MiniSparkline({
  values,
  width = 80,
  height = 24,
  stroke,
  label,
  className = "",
}: MiniSparklineProps) {
  const { path, color, ariaLabel } = useMemo(() => {
    if (!values || values.length < 2) {
      return { path: "", color: "#71717A", ariaLabel: label ?? "No data" };
    }
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min || 1;
    const stepX = width / (values.length - 1);
    const points = values.map((v, i) => {
      const x = i * stepX;
      const y = height - ((v - min) / range) * height;
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    });
    const d = `M ${points.join(" L ")}`;
    const up = values[values.length - 1] >= values[0];
    const direction = up ? "up" : "down";
    return {
      path: d,
      color: stroke ?? (up ? "#10B981" : "#EF4444"),
      ariaLabel:
        label ??
        `Trend ${direction}, ${values.length} data points, low ${min.toFixed(
          2
        )}, high ${max.toFixed(2)}`,
    };
  }, [values, width, height, stroke, label]);

  if (!path) {
    return (
      <span
        role="img"
        aria-label={ariaLabel}
        className={`inline-block text-[10px] text-zinc-600 ${className}`}
        style={{ width, height, lineHeight: `${height}px` }}
      >
        —
      </span>
    );
  }

  return (
    <svg
      role="img"
      aria-label={ariaLabel}
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className={`inline-block ${className}`}
      preserveAspectRatio="none"
    >
      <path
        d={path}
        fill="none"
        stroke={color}
        strokeWidth={1.25}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
