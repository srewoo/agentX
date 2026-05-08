import { useMemo } from "react";
import { formatPct } from "@/lib/format";

export interface SectorTile {
  /** NIFTY sector name e.g. "Nifty Bank", "Nifty IT" */
  sector: string;
  /** Average conviction across recommendations (0–100) — drives tile colour by default. */
  avgConviction: number;
  /** Day's % change for the sector index (used in tooltip + secondary line). */
  dayChangePct: number;
  /** Number of contributing recommendations. */
  count: number;
}

export type HeatmapMode = "conviction" | "dayChange";

export interface SectorHeatmapProps {
  tiles: readonly SectorTile[];
  mode?: HeatmapMode;
  /** Called when a tile is activated (click / Enter / Space). */
  onSelect?: (sector: string) => void;
  className?: string;
}

function clamp01(n: number): number {
  if (Number.isNaN(n)) return 0;
  if (n < 0) return 0;
  if (n > 1) return 1;
  return n;
}

function colorForConviction(v: number): string {
  // 0..100 mapped: 0 → red, 50 → amber, 100 → green
  if (v >= 70) return "bg-rec-success/30 text-rec-success border-rec-success/40";
  if (v >= 50) return "bg-rec-success/15 text-rec-success border-rec-success/25";
  if (v >= 35) return "bg-rec-warn/20 text-rec-warn border-rec-warn/30";
  return "bg-rec-danger/20 text-rec-danger border-rec-danger/30";
}

function colorForChange(v: number): string {
  if (v >= 1.5) return "bg-rec-success/30 text-rec-success border-rec-success/40";
  if (v > 0) return "bg-rec-success/15 text-rec-success border-rec-success/25";
  if (v > -1.5) return "bg-rec-danger/20 text-rec-danger border-rec-danger/30";
  return "bg-rec-danger/40 text-rec-danger border-rec-danger/50";
}

/**
 * NIFTY sector grid coloured by average conviction (default) or day change.
 * Each tile is keyboard-focusable and exposes its sector name + values via aria-label.
 */
export default function SectorHeatmap({
  tiles,
  mode = "conviction",
  onSelect,
  className,
}: SectorHeatmapProps) {
  const sorted = useMemo(() => {
    return [...tiles].sort((a, b) =>
      mode === "conviction" ? b.avgConviction - a.avgConviction : b.dayChangePct - a.dayChangePct
    );
  }, [tiles, mode]);

  if (sorted.length === 0) {
    return (
      <div
        className={[
          "p-3 rounded-lg border border-rec-border text-[11px] text-rec-fg-muted text-center",
          className ?? "",
        ].join(" ")}
      >
        No sector data available.
      </div>
    );
  }

  return (
    <div
      className={["grid grid-cols-3 gap-1.5", className ?? ""].join(" ")}
      role="grid"
      aria-label={`Sector heatmap by ${mode === "conviction" ? "average conviction" : "day change"}`}
    >
      {sorted.map((t) => {
        const tone = mode === "conviction" ? colorForConviction(t.avgConviction) : colorForChange(t.dayChangePct);
        // Soft intensity scale on top of tone (opacity weight via inline style for granularity).
        const intensity =
          mode === "conviction" ? clamp01(t.avgConviction / 100) : clamp01(Math.abs(t.dayChangePct) / 3);
        const aria = `${t.sector}, ${t.count} recommendations, average conviction ${Math.round(
          t.avgConviction
        )}, day change ${formatPct(t.dayChangePct)}`;
        const interactive = Boolean(onSelect);
        return (
          <button
            key={t.sector}
            type="button"
            role="gridcell"
            tabIndex={interactive ? 0 : -1}
            onClick={interactive ? () => onSelect!(t.sector) : undefined}
            disabled={!interactive}
            aria-label={aria}
            title={aria}
            className={[
              "rounded-md border px-2 py-1.5 text-left transition-colors",
              tone,
              interactive
                ? "cursor-pointer hover:brightness-125 focus:outline-none focus-visible:ring-2 focus-visible:ring-rec-focus"
                : "cursor-default",
            ].join(" ")}
            style={{ opacity: 0.6 + intensity * 0.4 }}
          >
            <div className="text-[11px] font-semibold leading-tight truncate">{t.sector}</div>
            <div className="flex items-baseline justify-between gap-1 mt-0.5 text-[10px] tabular-nums">
              <span>{Math.round(t.avgConviction)}</span>
              <span className="opacity-80">{formatPct(t.dayChangePct)}</span>
            </div>
          </button>
        );
      })}
    </div>
  );
}
