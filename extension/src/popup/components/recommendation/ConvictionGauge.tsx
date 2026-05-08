import { useEffect, useRef, useState } from "react";

export type ConvictionGaugeSize = "sm" | "md" | "lg";

export interface ConvictionGaugeProps {
  /** 0–100 conviction score */
  value: number;
  size?: ConvictionGaugeSize;
  /** Optional accessible label override */
  label?: string;
  /** Show numeric value inside the ring (default: true for md/lg) */
  showValue?: boolean;
  className?: string;
}

const SIZE_MAP: Record<ConvictionGaugeSize, { px: number; stroke: number; font: string }> = {
  sm: { px: 28, stroke: 4, font: "text-[10px]" },
  md: { px: 56, stroke: 6, font: "text-sm" },
  lg: { px: 88, stroke: 8, font: "text-lg" },
};

function clamp(n: number): number {
  if (Number.isNaN(n)) return 0;
  if (n < 0) return 0;
  if (n > 100) return 100;
  return n;
}

function colorFor(value: number): { stroke: string; text: string } {
  if (value < 40) return { stroke: "var(--rec-danger, #ef4444)", text: "text-red-400" };
  if (value < 70) return { stroke: "var(--rec-warn, #f59e0b)", text: "text-amber-400" };
  return { stroke: "var(--rec-success, #10b981)", text: "text-emerald-400" };
}

function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * Circular gauge for 0–100 conviction. Color shifts: red < 40, amber 40–70, green > 70.
 * Animates on mount unless the user prefers reduced motion.
 */
export default function ConvictionGauge({
  value,
  size = "md",
  label,
  showValue,
  className,
}: ConvictionGaugeProps) {
  const safe = clamp(value);
  const { px, stroke, font } = SIZE_MAP[size];
  const radius = (px - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const reduce = useRef(prefersReducedMotion());
  const [animated, setAnimated] = useState(reduce.current ? safe : 0);
  const { stroke: strokeColor, text } = colorFor(safe);
  const display = showValue ?? size !== "sm";

  useEffect(() => {
    if (reduce.current) {
      setAnimated(safe);
      return;
    }
    let raf = 0;
    const start = performance.now();
    const duration = 600;
    const from = animated;
    const tick = (t: number) => {
      const k = Math.min(1, (t - start) / duration);
      const eased = 1 - Math.pow(1 - k, 3);
      setAnimated(from + (safe - from) * eased);
      if (k < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [safe]);

  const offset = circumference * (1 - animated / 100);
  const a11yLabel = label ?? `Conviction ${Math.round(safe)} out of 100`;

  return (
    <div
      className={["relative inline-flex items-center justify-center", className].filter(Boolean).join(" ")}
      style={{ width: px, height: px }}
      role="img"
      aria-label={a11yLabel}
    >
      <svg width={px} height={px} viewBox={`0 0 ${px} ${px}`} aria-hidden="true">
        <circle
          cx={px / 2}
          cy={px / 2}
          r={radius}
          fill="none"
          stroke="var(--rec-track, rgba(255,255,255,0.08))"
          strokeWidth={stroke}
        />
        <circle
          cx={px / 2}
          cy={px / 2}
          r={radius}
          fill="none"
          stroke={strokeColor}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          transform={`rotate(-90 ${px / 2} ${px / 2})`}
        />
      </svg>
      {display && (
        <span className={["absolute font-semibold tabular-nums", font, text].join(" ")}>
          {Math.round(animated)}
        </span>
      )}
    </div>
  );
}
