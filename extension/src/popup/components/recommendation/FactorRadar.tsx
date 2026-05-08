import { useMemo } from "react";
import type { FactorSignal } from "./types";

export type Signal = FactorSignal;

export interface FactorRadarProps {
  signals: readonly Signal[];
  size?: number;
  className?: string;
  /** Minimum factors to display — pads with zero-axes when signal list is short. */
  minAxes?: number;
}

const DEFAULT_AXES = [
  "trend",
  "momentum",
  "volume",
  "fno",
  "flows",
  "sector",
  "news",
  "volatility",
] as const;

interface Axis {
  name: string;
  /** Weighted score normalised to 0..1 */
  score: number;
  direction: Signal["direction"];
}

function normalizeScore(s: Signal): number {
  // weight × value, both expected ~0..1 or scaled small numbers; clamp.
  const raw = (s.weight ?? 0) * (s.value ?? 0);
  if (Number.isNaN(raw)) return 0;
  if (raw <= 0) return 0;
  if (raw >= 1) return 1;
  return raw;
}

/**
 * Compact radar showing each contributing factor. Polygon area encodes overall weight,
 * point colour encodes positive / negative / neutral direction.
 */
export default function FactorRadar({ signals, size = 140, className, minAxes = 6 }: FactorRadarProps) {
  const axes = useMemo<Axis[]>(() => {
    const provided = signals.map<Axis>((s) => ({
      name: s.name,
      score: normalizeScore(s),
      direction: s.direction,
    }));
    if (provided.length >= minAxes) return provided;
    // Pad with zero-score axes from default list, skipping names already present.
    const present = new Set(provided.map((p) => p.name.toLowerCase()));
    const padding: Axis[] = [];
    for (const name of DEFAULT_AXES) {
      if (present.has(name)) continue;
      padding.push({ name, score: 0, direction: "neu" });
      if (provided.length + padding.length >= minAxes) break;
    }
    return [...provided, ...padding];
  }, [signals, minAxes]);

  const cx = size / 2;
  const cy = size / 2;
  const radius = size / 2 - 14;
  const n = axes.length;

  const points = axes.map((a, i) => {
    const angle = (Math.PI * 2 * i) / n - Math.PI / 2;
    const r = radius * a.score;
    return {
      x: cx + Math.cos(angle) * r,
      y: cy + Math.sin(angle) * r,
      labelX: cx + Math.cos(angle) * (radius + 8),
      labelY: cy + Math.sin(angle) * (radius + 8),
      axis: a,
      angle,
    };
  });

  const ringRadii = [0.25, 0.5, 0.75, 1];
  const polygon = points.map((p) => `${p.x.toFixed(2)},${p.y.toFixed(2)}`).join(" ");

  const directionColor: Record<Signal["direction"], string> = {
    pos: "var(--rec-success, #10b981)",
    neg: "var(--rec-danger, #ef4444)",
    neu: "var(--rec-neutral, #94a3b8)",
  };

  return (
    <div
      className={["inline-block", className ?? ""].join(" ")}
      role="img"
      aria-label={`Factor radar across ${n} factors: ${axes.map((a) => `${a.name} ${(a.score * 100).toFixed(0)}%`).join(", ")}`}
    >
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        {/* Concentric rings */}
        {ringRadii.map((rk) => (
          <circle
            key={rk}
            cx={cx}
            cy={cy}
            r={radius * rk}
            fill="none"
            stroke="var(--rec-track, rgba(255,255,255,0.06))"
            strokeWidth={1}
          />
        ))}
        {/* Spokes */}
        {points.map((p, i) => (
          <line
            key={`sp-${i}`}
            x1={cx}
            y1={cy}
            x2={cx + Math.cos(p.angle) * radius}
            y2={cy + Math.sin(p.angle) * radius}
            stroke="var(--rec-track, rgba(255,255,255,0.06))"
            strokeWidth={1}
          />
        ))}
        {/* Filled polygon */}
        <polygon
          points={polygon}
          fill="var(--rec-info, #3b82f6)"
          fillOpacity={0.18}
          stroke="var(--rec-info, #3b82f6)"
          strokeWidth={1.25}
        />
        {/* Points */}
        {points.map((p, i) => (
          <circle
            key={`pt-${i}`}
            cx={p.x}
            cy={p.y}
            r={2.5}
            fill={directionColor[p.axis.direction]}
          />
        ))}
        {/* Labels */}
        {points.map((p, i) => (
          <text
            key={`lbl-${i}`}
            x={p.labelX}
            y={p.labelY}
            textAnchor="middle"
            dominantBaseline="middle"
            className="fill-rec-fg-muted"
            style={{ fontSize: 9 }}
          >
            {p.axis.name}
          </text>
        ))}
      </svg>
    </div>
  );
}
